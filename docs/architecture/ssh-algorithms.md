# SSH algorithm matrix

Every algorithm otto's SSH stack can negotiate, from the asyncssh release
the lock pins — key exchange, host key, cipher, MAC and compression.
{doc}`support-matrix` answers "can otto do X against a device like mine"
from runs against real targets; this page answers the same question for the
SSH handshake, but pinned by the unit suite against the locked
asyncssh/cryptography rather than measured by the bed. "Stock" means a host
with no `ssh_options` (see
[Legacy SSH servers](../configuration/settings.md#legacy-ssh-servers) for
the TOML shape) already offers it; every other row needs the matching
`ssh_options` list added to the host, or to a `[host_preferences]` block
covering it. Every row on this page is pinned by the unit suite against the
*installed* asyncssh and cryptography — that it is registered, that "Stock"
matches what asyncssh offers by default — and, for every row whose *Needs*
column is empty (or names a library this interpreter has), that an
in-process handshake can actually negotiate it; rows needing a Kerberos
credential or a FIDO key are pinned as registered and stock only — so a
dependency bump that drops one fails the suite before any device notices,
for every row the suite can actually drive a handshake over.

## Key exchange

Set with `kex_algs`.

| Algorithm | Stock | Needs |
|---|---|---|
| `gss-curve25519-sha256` | yes | Kerberos credentials on the client |
| `gss-curve448-sha512` | yes | Kerberos credentials on the client |
| `gss-nistp521-sha512` | yes | Kerberos credentials on the client |
| `gss-nistp384-sha384` | yes | Kerberos credentials on the client |
| `gss-nistp256-sha256` | yes | Kerberos credentials on the client |
| `gss-1.3.132.0.10-sha256` | yes | Kerberos credentials on the client |
| `gss-gex-sha256` | yes | Kerberos credentials on the client |
| `gss-gex-sha1` | no | Kerberos credentials on the client |
| `gss-group14-sha256` | yes | Kerberos credentials on the client |
| `gss-group15-sha512` | yes | Kerberos credentials on the client |
| `gss-group16-sha512` | yes | Kerberos credentials on the client |
| `gss-group17-sha512` | yes | Kerberos credentials on the client |
| `gss-group18-sha512` | yes | Kerberos credentials on the client |
| `gss-group14-sha1` | yes | Kerberos credentials on the client |
| `gss-group1-sha1` | no | Kerberos credentials on the client |
| `mlkem768x25519-sha256` | yes | |
| `mlkem768nistp256-sha256` | yes | |
| `mlkem1024nistp384-sha384` | yes | |
| `sntrup761x25519-sha512` | yes | liboqs importable when asyncssh loads |
| `sntrup761x25519-sha512@openssh.com` | yes | liboqs importable when asyncssh loads |
| `curve25519-sha256` | yes | |
| `curve25519-sha256@libssh.org` | yes | |
| `curve448-sha512` | yes | |
| `ecdh-sha2-nistp521` | yes | |
| `ecdh-sha2-nistp384` | yes | |
| `ecdh-sha2-nistp256` | yes | |
| `ecdh-sha2-1.3.132.0.10` | yes | |
| `diffie-hellman-group-exchange-sha256` | yes | |
| `diffie-hellman-group-exchange-sha224@ssh.com` | no | |
| `diffie-hellman-group-exchange-sha384@ssh.com` | no | |
| `diffie-hellman-group-exchange-sha512@ssh.com` | no | |
| `diffie-hellman-group-exchange-sha1` | no | |
| `diffie-hellman-group14-sha256` | yes | |
| `diffie-hellman-group15-sha512` | yes | |
| `diffie-hellman-group16-sha512` | yes | |
| `diffie-hellman-group17-sha512` | yes | |
| `diffie-hellman-group18-sha512` | yes | |
| `diffie-hellman-group14-sha256@ssh.com` | yes | |
| `diffie-hellman-group14-sha224@ssh.com` | no | |
| `diffie-hellman-group15-sha256@ssh.com` | no | |
| `diffie-hellman-group15-sha384@ssh.com` | no | |
| `diffie-hellman-group16-sha384@ssh.com` | no | |
| `diffie-hellman-group16-sha512@ssh.com` | no | |
| `diffie-hellman-group18-sha512@ssh.com` | no | |
| `diffie-hellman-group14-sha1` | yes | |
| `diffie-hellman-group1-sha1` | no | |
| `rsa2048-sha256` | yes | |
| `rsa1024-sha1` | no | |

## Host key

Set with `server_host_key_algs`.

| Algorithm | Stock | Needs |
|---|---|---|
| `rsa-sha2-256` | yes | |
| `rsa-sha2-512` | yes | |
| `ssh-rsa-sha224@ssh.com` | yes | |
| `ssh-rsa-sha256@ssh.com` | yes | |
| `ssh-rsa-sha384@ssh.com` | yes | |
| `ssh-rsa-sha512@ssh.com` | yes | |
| `ssh-rsa` | yes | |
| `sk-ssh-ed25519@openssh.com` | yes | a FIDO security key |
| `sk-ecdsa-sha2-nistp256@openssh.com` | yes | a FIDO security key |
| `webauthn-sk-ecdsa-sha2-nistp256@openssh.com` | yes | a FIDO security key |
| `ssh-ed25519` | yes | |
| `ssh-ed448` | yes | |
| `ecdsa-sha2-nistp521` | yes | |
| `ecdsa-sha2-nistp384` | yes | |
| `ecdsa-sha2-nistp256` | yes | |
| `ecdsa-sha2-1.3.132.0.10` | yes | |
| `ssh-dss` | no | |

## Cipher

Set with `encryption_algs`.

| Algorithm | Stock | Needs |
|---|---|---|
| `chacha20-poly1305@openssh.com` | yes | |
| `aes256-gcm@openssh.com` | yes | |
| `aes128-gcm@openssh.com` | yes | |
| `aes256-ctr` | yes | |
| `aes192-ctr` | yes | |
| `aes128-ctr` | yes | |
| `aes256-cbc` | no | |
| `aes192-cbc` | no | |
| `aes128-cbc` | no | |
| `3des-cbc` | no | |
| `blowfish-cbc` | no | |
| `cast128-cbc` | no | |
| `seed-cbc@ssh.com` | no | |
| `arcfour256` | no | |
| `arcfour128` | no | |
| `arcfour` | no | |

## MAC

Set with `mac_algs`.

| Algorithm | Stock | Needs |
|---|---|---|
| `umac-64-etm@openssh.com` | yes | libnettle importable when asyncssh loads |
| `umac-128-etm@openssh.com` | yes | libnettle importable when asyncssh loads |
| `hmac-sha2-256-etm@openssh.com` | yes | |
| `hmac-sha2-512-etm@openssh.com` | yes | |
| `hmac-sha1-etm@openssh.com` | yes | |
| `hmac-md5-etm@openssh.com` | no | |
| `hmac-sha2-256-96-etm@openssh.com` | no | |
| `hmac-sha2-512-96-etm@openssh.com` | no | |
| `hmac-sha1-96-etm@openssh.com` | no | |
| `hmac-md5-96-etm@openssh.com` | no | |
| `umac-64@openssh.com` | yes | libnettle importable when asyncssh loads |
| `umac-128@openssh.com` | yes | libnettle importable when asyncssh loads |
| `hmac-sha2-256` | yes | |
| `hmac-sha2-512` | yes | |
| `hmac-sha1` | yes | |
| `hmac-sha256-2@ssh.com` | yes | |
| `hmac-sha224@ssh.com` | yes | |
| `hmac-sha256@ssh.com` | yes | |
| `hmac-sha384@ssh.com` | yes | |
| `hmac-sha512@ssh.com` | yes | |
| `hmac-md5` | no | |
| `hmac-sha2-256-96` | no | |
| `hmac-sha2-512-96` | no | |
| `hmac-sha1-96` | no | |
| `hmac-md5-96` | no | |

## Compression

Set with `compression_algs`.

| Algorithm | Stock | Needs |
|---|---|---|
| `none` | yes | |
| `zlib@openssh.com` | yes | |
| `zlib` | no | |
