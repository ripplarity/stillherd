# DOCKER_DOWN

The docker daemon was not reachable, so no container was started and no egress attestation was produced.

`docker info` error:

    failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.

Compose files were downloaded and validated only (see recipes/*/validation.txt).
