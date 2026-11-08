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
