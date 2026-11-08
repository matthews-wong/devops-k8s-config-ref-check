# k8s-config-ref-check

A small web workload and a checker for the references between its manifests.

`kubeconform` validates each file against its schema, but it cannot tell that a
Deployment mounts a ConfigMap nobody wrote, or that a Service selector matches
no pod. Those mistakes only show up as `CreateContainerConfigError` or an empty
endpoint list on a cluster. `scripts/check_refs.py` catches them from the
manifests alone.

## Layout

- `manifests/` namespace, config map, deployment, service
- `scripts/check_refs.py` the reference checker
- `scripts/test_check_refs.py` one failing case per kind of dangling reference

## What the checker covers

- `serviceAccountName`, ConfigMap, Secret and PersistentVolumeClaim references
  from `env`, `envFrom`, volumes and projected volumes, in the workload's namespace
- the keys behind `configMapKeyRef`, `secretKeyRef` and volume `items`
- `volumeMounts` that name no declared volume
- Service selectors that match no workload, and workload selectors that do not
  match their own pod template

`optional: true` references are skipped. The `default` service account is
assumed to exist.

## Secret created out of band

The deployment reads `API_TOKEN` from a Secret named `web-credentials` with the
key `api-token`. The Secret is not in this repo. Create it before applying:

```sh
kubectl -n refs-demo create secret generic web-credentials --from-literal=api-token=...
```

The checker is told about it with `--external Secret/web-credentials` (the
`EXTERNAL` variable in the Makefile). Without that flag it reports the Secret
as missing.

## Config map rollouts

The config map is `immutable` and carries a version in its name. To change the
config, add `web-config-v2`, point the deployment at it, then delete v1. The
checker fails the change if either reference is left behind.

## Validate

```sh
make validate
```

Runs `kubeconform -strict`, the reference checker, the unit tests and
`actionlint`. Needs `kubeconform`, `actionlint`, Python 3 and PyYAML.

## Limits

Only objects in the files passed to the checker are known. Cluster-scoped
lookups, objects from CRDs and Helm or Kustomize output are out of scope, and
nothing here was applied to a live cluster.
