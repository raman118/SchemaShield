#!/bin/sh
# Creates only dedicated local development Docker credentials; no package/repo commands.
set -eu
umask 077
TASK_CERT_DIR="$(dirname "$0")/../.secrets/docker"
mkdir -p "$TASK_CERT_DIR"
cd "$TASK_CERT_DIR"
if [ -e ca-key.pem ] || [ -e key.pem ]; then
  echo 'Dedicated TLS credentials already exist; refusing to overwrite.' >&2
  exit 1
fi
openssl genrsa -out ca-key.pem 4096
openssl req -new -x509 -days 365 -key ca-key.pem -sha256 -out ca.pem -subj '/CN=SchemaShield local Docker CA'
openssl genrsa -out server-key.pem 4096
openssl req -new -key server-key.pem -out server.csr -subj '/CN=localhost'
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > server.ext
openssl x509 -req -days 365 -sha256 -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -out server-cert.pem -extfile server.ext
openssl genrsa -out key.pem 4096
openssl req -new -key key.pem -out client.csr -subj '/CN=SchemaShield local Docker client'
printf 'extendedKeyUsage=clientAuth\n' > client.ext
openssl x509 -req -days 365 -sha256 -in client.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -out cert.pem -extfile client.ext
chmod 600 *key.pem
echo 'Dedicated mutual TLS Docker credentials created.'
