# Self-host boot attestations

Each boot ran through a local HTTP(S) proxy that refuses every host except localhost and records attempts. Boot window: compose up, then polling one health URL until it returned 2xx. Bytes out = request bytes forwarded to hosts outside the allowlist. Attestation = ed25519 signature checked by silent/verify.sh.

| Recipe | Image digests | External hosts contacted during boot and smoke | Bytes out | verify.sh |
|---|---|---|---|---|
| hoppscotch | hoppscotch/hoppscotch@sha256:09a50aa1831ff1e39dfe73bf672e2bca45e1e73514d656e9be6a91f561071a53 postgres@sha256:1aac5b49cefd627f02816141ca4ad012d6cc19460dfe02a9b5168a105be3fc53  | none recorded | 0 | OK |
| rocketchat | mongodb/mongodb-community-server@sha256:26948caea975097b3a381ad9c2d531fccebdb826a5c65e3ca274b08d3cfdf987 nats@sha256:e4bf19f15fd3218814a4e3c9e0064e1334bd8aa20d5984b9f1a0afd084f8cc00 natsio/prometheus-nats-exporter@sha256:26c826662ac8424597cc9bdf89ea5b606eb66e3c11db9b1215c27d2076bbb01b percona/mongodb_exporter@sha256:d9c577166068197bf6908296d9742cc8267e7b7e1054199436d6d922e8921679 registry.rocket.chat/rocketchat/rocket.chat@sha256:700a9ac5a665bae9adab4da1a87adc87e241b405a61b01de80bb396f01b3714e  | collector.rocket.chat, releases.rocket.chat (requests refused by the proxy) | 0 | OK |
| appflowy | not pulled: image minio/minio not found on Docker Hub, quay.io/minio/minio refused with 401 | not booted | not booted | no attestation (see appflowy.FAILED.md) |

Notes: the hoppscotch recipe uses an override file that runs the published all-in-one image because the upstream compose builds from a Dockerfile that is not in the downloaded file set; the same override runs the image's own database migration job before the application starts. The hoppscotch health check is the backend's /ping on port 3170 (an earlier run of this row polled only the web front end on port 3000, while the backend inside the image had not started because its tables did not exist; that run was replaced). The rocketchat recipe adds the upstream database and messaging compose files. Traffic that does not use the proxy environment variables is not visible to the proxy (see silent/README.md).
