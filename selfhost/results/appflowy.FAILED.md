# appflowy: not booted (2 attempts)

Attempt 1, upstream compose unchanged: `docker compose pull` failed for the minio service.

    Error response from daemon: pull access denied for minio/minio, repository does not exist or may require 'docker login'

Attempt 2, override pointing minio at `quay.io/minio/minio:latest`:

    Error response from daemon: unknown: failed to resolve reference "quay.io/minio/minio:latest": unexpected status from HEAD request to https://quay.io/v2/minio/minio/manifests/latest: 401 UNAUTHORIZED

No container started, so no boot attestation exists for this recipe. The failed-run attestation was removed.
