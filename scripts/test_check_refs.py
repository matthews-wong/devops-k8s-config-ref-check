import contextlib
import copy
import io
import os
import unittest

import check_refs
from check_refs import Doc

MANIFESTS = os.path.join(os.path.dirname(__file__), "..", "manifests")
EXTERNAL = frozenset({"Secret/web-credentials"})


def load_manifests():
    files = sorted(
        os.path.join(MANIFESTS, name) for name in os.listdir(MANIFESTS) if name.endswith(".yaml")
    )
    return check_refs.load_docs(files)


def mutate(docs, kind, change):
    """Return docs with `change` applied to a deep copy of the one of `kind`."""
    out = []
    for doc in docs:
        if doc.kind != kind:
            out.append(doc)
            continue
        body = copy.deepcopy(doc.body)
        change(body)
        out.append(Doc(doc.file, doc.kind, doc.namespace, doc.name, body))
    return out


def pod_spec(body):
    return body["spec"]["template"]["spec"]


class CheckRefsTest(unittest.TestCase):
    def problems(self, docs, external=EXTERNAL):
        return list(check_refs.check(docs, external))

    def test_shipped_manifests_are_clean(self):
        self.assertEqual(self.problems(load_manifests()), [])

    def test_undeclared_external_secret_is_reported(self):
        found = self.problems(load_manifests(), external=frozenset())
        self.assertEqual(len(found), 1)
        self.assertIn("Secret 'web-credentials' not found", found[0])

    def test_missing_service_account(self):
        docs = [d for d in load_manifests() if d.kind != "ServiceAccount"]
        found = self.problems(docs)
        self.assertEqual(len(found), 1)
        self.assertIn("ServiceAccount 'web' not found", found[0])

    def test_default_service_account_needs_no_manifest(self):
        def use_default(body):
            pod_spec(body)["serviceAccountName"] = "default"
        docs = [d for d in load_manifests() if d.kind != "ServiceAccount"]
        self.assertEqual(self.problems(mutate(docs, "Deployment", use_default)), [])

    def test_missing_config_map_for_volume_and_env(self):
        docs = [d for d in load_manifests() if d.kind != "ConfigMap"]
        found = self.problems(docs)
        self.assertEqual(len(found), 2)
        self.assertTrue(any("env LOG_LEVEL" in m for m in found))
        self.assertTrue(any("volume config" in m for m in found))

    def test_missing_config_map_key(self):
        def rename(body):
            ref = pod_spec(body)["containers"][0]["env"][0]["valueFrom"]["configMapKeyRef"]
            ref["key"] = "LOG_LVL"
        found = self.problems(mutate(load_manifests(), "Deployment", rename))
        self.assertEqual(len(found), 1)
        self.assertIn("key 'LOG_LVL' not in ConfigMap 'web-config-v1'", found[0])

    def test_missing_volume_item_key(self):
        def rename(body):
            pod_spec(body)["volumes"][0]["configMap"]["items"][0]["key"] = "nginx.conf"
        found = self.problems(mutate(load_manifests(), "Deployment", rename))
        self.assertEqual(len(found), 1)
        self.assertIn("key 'nginx.conf' not in ConfigMap", found[0])

    def test_optional_reference_is_skipped(self):
        def make_optional(body):
            container = pod_spec(body)["containers"][0]
            container["env"][1]["valueFrom"]["secretKeyRef"]["optional"] = True
        docs = mutate(load_manifests(), "Deployment", make_optional)
        self.assertEqual(self.problems(docs, external=frozenset()), [])

    def test_missing_pvc(self):
        def add_claim(body):
            pod_spec(body)["volumes"].append(
                {"name": "data", "persistentVolumeClaim": {"claimName": "web-data"}})
        found = self.problems(mutate(load_manifests(), "Deployment", add_claim))
        self.assertEqual(len(found), 1)
        self.assertIn("PersistentVolumeClaim 'web-data' not found", found[0])

    def test_projected_secret_is_checked(self):
        def project(body):
            pod_spec(body)["volumes"].append({
                "name": "creds",
                "projected": {"sources": [{"secret": {"name": "other-creds"}}]},
            })
        found = self.problems(mutate(load_manifests(), "Deployment", project))
        self.assertEqual(len(found), 1)
        self.assertIn("Secret 'other-creds' not found", found[0])

    def test_volume_mount_without_volume(self):
        def drop_volume(body):
            pod_spec(body)["volumes"] = [
                v for v in pod_spec(body)["volumes"] if v["name"] != "cache"]
        found = self.problems(mutate(load_manifests(), "Deployment", drop_volume))
        self.assertEqual(len(found), 1)
        self.assertIn("volumeMount 'cache' has no matching volume", found[0])

    def test_reference_in_another_namespace_is_not_found(self):
        docs = [
            Doc(d.file, d.kind, "elsewhere", d.name, d.body) if d.kind == "ConfigMap" else d
            for d in load_manifests()
        ]
        self.assertEqual(len(self.problems(docs)), 2)

    def test_service_selector_matching_nothing(self):
        def retarget(body):
            body["spec"]["selector"]["app.kubernetes.io/name"] = "api"
        found = self.problems(mutate(load_manifests(), "Service", retarget))
        self.assertEqual(len(found), 1)
        self.assertIn("matches no workload", found[0])

    def test_service_without_selector_is_skipped(self):
        def external_name(body):
            del body["spec"]["selector"]
        self.assertEqual(self.problems(mutate(load_manifests(), "Service", external_name)), [])

    def test_workload_selector_must_match_template(self):
        def drift(body):
            body["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] = "api"
        found = self.problems(mutate(load_manifests(), "Deployment", drift))
        self.assertTrue(any("does not match pod template labels" in m for m in found))


class MainTest(unittest.TestCase):
    def run_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return check_refs.main(list(args))

    def test_exit_codes(self):
        deployment = os.path.join(MANIFESTS, "deployment.yaml")
        self.assertEqual(self.run_main(deployment), 1)
        self.assertEqual(self.run_main("--external", "Secret/web-credentials", MANIFESTS + "/service.yaml"), 1)
        files = sorted(os.path.join(MANIFESTS, n) for n in os.listdir(MANIFESTS))
        self.assertEqual(self.run_main("--external", "Secret/web-credentials", *files), 0)


if __name__ == "__main__":
    unittest.main()
