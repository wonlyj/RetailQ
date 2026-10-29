# syntax=docker/dockerfile:1
FROM alpine:3.20
RUN apk add --no-cache ca-certificates
ADD --checksum=sha256:5708e53878085ab263c333b24fa7e59980425d64a9b085996b58799f9645e1db https://github.com/minio/minio/releases/download/RELEASE.2024-05-28T17-19-04Z/minio.linux-amd64.RELEASE.2024-05-28T17-19-04Z /usr/local/bin/minio
RUN chmod 0755 /usr/local/bin/minio
ENTRYPOINT ["minio"]
