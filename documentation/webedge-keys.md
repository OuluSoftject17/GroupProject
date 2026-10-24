# WebEdge Keys

WebEdge has two key pairs: an ML-KEM-768 key for post-quantum key
establishment, and an RSA-2048 key for legacy clients. To make the keys
survive restarts, and so that clients can pin them, both are loaded from
files.

## Generating keys

```
poetry run python webedge.py generate-keys keys
```

This writes four files to the `keys` directory and prints the ML-KEM
fingerprint:

| File | Contents | Secret |
|------|----------|--------|
| `webedge-kem-private.pem` | ML-KEM-768 private key (PKCS#8 PEM) | yes |
| `webedge-rsa-private.pem` | RSA-2048 private key (PKCS#8 PEM) | yes |
| `webedge-kem-public.pem` | ML-KEM-768 public key | no |
| `webedge-rsa-public.pem` | RSA-2048 public key | no |

The command refuses to overwrite existing files, so it cannot replace a key
that clients have already pinned.

The private key files are not encrypted. On Linux and macOS they are created
with permissions `0600` (readable only by the owner). On Windows, the
permissions are not set automatically: keep the files in a folder that only
the user running WebEdge can read.

`keys/` and `*-private.pem` are listed in `.gitignore`. Never commit a private
key. If one is committed by accident, treat it as compromised and rotate it
(see below).

## Giving the public key to clients

Clients must get WebEdge's ML-KEM public key, or its fingerprint, **before**
they connect, over a channel they already trust. For example, it can be
installed together with the client software, or copied by an administrator.

The fingerprint is the SHA-256 of the raw ML-KEM public key bytes, in hex.
`generate-keys` prints it, and WebEdge logs it at startup:

```
INFO:webedge:ML-KEM public key fingerprint (SHA-256): 42346fca02c4...
```

A client can compute it from the public key file:

```python
import hashlib
from cryptography.hazmat.primitives import serialization

with open("webedge-kem-public.pem", "rb") as key_file:
    public_key = serialization.load_pem_public_key(key_file.read())

print(hashlib.sha256(public_key.public_bytes_raw()).hexdigest())
```

WebEdge also serves its public key at `GET /kem-public-key`. That response
comes over plain HTTP, so **it must never be trusted on its own**: a
man-in-the-middle can replace it with their own key. A client that fetches
the key there must compare its fingerprint with the pinned one and stop if
they differ.

Legacy RSA clients need `webedge-rsa-public.pem` in the same way.

## Passing the key files to WebEdge

WebEdge reads the paths of the private keys from two environment variables:

```
WEBEDGE_KEM_KEY_FILE=keys/webedge-kem-private.pem
WEBEDGE_RSA_KEY_FILE=keys/webedge-rsa-private.pem
```

* If a variable is set but the file is missing, corrupt, or holds the wrong
  type of key, WebEdge refuses to start.
* If a variable is not set, WebEdge creates a temporary key and logs a
  warning. A temporary key changes on every restart, so clients cannot pin
  it. Use this only for local development and tests.

## Rotating keys by hand

Rotate the keys regularly, and immediately if a private key may have leaked.

1. Generate new keys into a new, empty directory:
   `poetry run python webedge.py generate-keys keys-new`
2. Give the new public key or fingerprint to all clients, while they still
   accept the old one.
3. Point `WEBEDGE_KEM_KEY_FILE` and `WEBEDGE_RSA_KEY_FILE` to the new files
   and restart WebEdge. Check the fingerprint in the log.
4. Remove the old public key from the clients, and delete the old private
   key files.

WebEdge holds only one key of each type at a time, so clients that have not
received the new key fail from step 3 onwards. If a key has leaked, do steps
2 and 3 straight away.

## Future work

Not covered here: signing the public key (for example with ML-DSA), TLS and
certificates, and automatic key rotation.
