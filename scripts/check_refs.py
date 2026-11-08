#!/usr/bin/env python3
"""Fail when a manifest points at an object that is not defined next to it.

Checks, per namespace:
  - serviceAccountName, ConfigMap, Secret and PersistentVolumeClaim references
    from env, envFrom, volumes and projected volumes, including the keys named
    by configMapKeyRef, secretKeyRef and volume `items`
  - every volumeMount names a declared volume
  - every Service selector matches the labels of at least one workload
  - every workload selector matches its own pod template labels

References marked `optional: true` are skipped. Objects that are created out of
band (a Secret from a vault, say) are declared with --external Kind/name.

Usage: scripts/check_refs.py [--external Kind/name]... [file...]
       (default files: manifests/*.yaml)
"""
import argparse
import glob
import sys
from dataclasses import dataclass

import yaml

DEFAULT_NAMESPACE = "default"
# Every namespace gets this account, so a manifest may name it without defining it.
DEFAULT_SERVICE_ACCOUNT = "default"
POD_SPEC_PATHS = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}
TEMPLATE_LABEL_PATHS = {
    kind: path[:-1] + ("metadata", "labels") for kind, path in POD_SPEC_PATHS.items()
}
SELECTOR_PATHS = {
    kind: ("spec", "selector", "matchLabels")
    for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet")
}
# Where each referenceable kind stores the keys a consumer may ask for.
KEY_FIELDS = {
    "ConfigMap": ("data", "binaryData"),
    "Secret": ("data", "stringData"),
}


@dataclass(frozen=True)
class Doc:
    file: str
    kind: str
    namespace: str
    name: str
    body: dict

    @property
    def label(self):
        return f"{self.file}: {self.kind}/{self.name}"


@dataclass(frozen=True)
class Ref:
    where: str
    kind: str
    name: str
    keys: tuple = ()
    optional: bool = False


def dig(doc, path):
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def load_docs(files):
    docs = []
    for file in files:
        with open(file, encoding="utf-8") as handle:
            for body in yaml.safe_load_all(handle):
                if not isinstance(body, dict) or "kind" not in body:
                    continue
                meta = body.get("metadata") or {}
                docs.append(Doc(
                    file, body["kind"], meta.get("namespace", DEFAULT_NAMESPACE),
                    meta.get("name", ""), body,
                ))
    return docs


def volume_refs(volume):
    """Yield the objects one volume (or projected source) depends on."""
    where = f"volume {volume.get('name', '?')}"
    for field, kind, name_field in (
        ("configMap", "ConfigMap", "name"),
        ("secret", "Secret", "secretName"),
    ):
        source = volume.get(field)
        if not source:
            continue
        # A projected secret/configMap source names its object with `name`.
        name = source.get(name_field) or source.get("name")
        keys = tuple(item["key"] for item in source.get("items") or [])
        yield Ref(where, kind, name, keys, bool(source.get("optional")))
    claim = volume.get("persistentVolumeClaim")
    if claim:
        yield Ref(where, "PersistentVolumeClaim", claim.get("claimName"))
    for source in dig(volume, ("projected", "sources")) or []:
        yield from volume_refs({"name": volume.get("name"), **source})


def env_refs(container):
    where = f"container {container.get('name', '?')}"
    for entry in container.get("envFrom") or []:
        for field, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
            ref = entry.get(field)
            if ref:
                yield Ref(f"{where} envFrom", kind, ref.get("name"), (), bool(ref.get("optional")))
    for entry in container.get("env") or []:
        source = entry.get("valueFrom") or {}
        for field, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
            ref = source.get(field)
            if ref:
                yield Ref(
                    f"{where} env {entry.get('name')}", kind, ref.get("name"),
                    (ref.get("key"),), bool(ref.get("optional")),
                )


def containers_of(pod_spec):
    return (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])


def pod_refs(pod_spec):
    account = pod_spec.get("serviceAccountName")
    if account and account != DEFAULT_SERVICE_ACCOUNT:
        yield Ref("pod", "ServiceAccount", account)
    for volume in pod_spec.get("volumes") or []:
        yield from volume_refs(volume)
    for container in containers_of(pod_spec):
        yield from env_refs(container)


def available_keys(target):
    keys = set()
    for field in KEY_FIELDS.get(target.kind, ()):
        keys |= set(target.body.get(field) or {})
    return keys


def check_ref(workload, ref, index, external):
    if ref.optional or f"{ref.kind}/{ref.name}" in external:
        return
    target = index.get((ref.kind, workload.namespace, ref.name))
    if target is None:
        yield (f"{workload.label}: {ref.where}: {ref.kind} {ref.name!r} "
               f"not found in namespace {workload.namespace}")
        return
    present = available_keys(target)
    for key in (key for key in ref.keys if key not in present):
        yield f"{workload.label}: {ref.where}: key {key!r} not in {ref.kind} {ref.name!r}"


def check_mounts(workload, pod_spec):
    declared = {volume.get("name") for volume in pod_spec.get("volumes") or []}
    for container in containers_of(pod_spec):
        for mount in container.get("volumeMounts") or []:
            if mount.get("name") not in declared:
                yield (f"{workload.label}: container {container.get('name', '?')}: "
                       f"volumeMount {mount.get('name')!r} has no matching volume")


def check_workload(workload, index, external):
    pod_spec = dig(workload.body, POD_SPEC_PATHS[workload.kind])
    if not isinstance(pod_spec, dict):
        return
    for ref in pod_refs(pod_spec):
        yield from check_ref(workload, ref, index, external)
    yield from check_mounts(workload, pod_spec)
    selector = dig(workload.body, SELECTOR_PATHS.get(workload.kind, ())) or {}
    labels = dig(workload.body, TEMPLATE_LABEL_PATHS[workload.kind]) or {}
    if not selector.items() <= labels.items():
        yield f"{workload.label}: selector {selector} does not match pod template labels {labels}"


def check_service(service, workloads):
    selector = dig(service.body, ("spec", "selector"))
    if not selector:
        return
    for workload in workloads:
        labels = dig(workload.body, TEMPLATE_LABEL_PATHS[workload.kind]) or {}
        if workload.namespace == service.namespace and selector.items() <= labels.items():
            return
    yield (f"{service.label}: selector {selector} matches no workload "
           f"in namespace {service.namespace}")


def check(docs, external=frozenset()):
    index = {(doc.kind, doc.namespace, doc.name): doc for doc in docs}
    workloads = [doc for doc in docs if doc.kind in POD_SPEC_PATHS]
    for workload in workloads:
        yield from check_workload(workload, index, external)
    for service in (doc for doc in docs if doc.kind == "Service"):
        yield from check_service(service, workloads)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--external", action="append", default=[], metavar="KIND/NAME",
                        help="object created outside these manifests")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    files = args.files or sorted(glob.glob("manifests/*.yaml"))
    if not files:
        print("check_refs: no manifests found", file=sys.stderr)
        return 2
    problems = list(check(load_docs(files), frozenset(args.external)))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"check_refs: ok ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
