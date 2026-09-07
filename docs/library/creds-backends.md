# Creds-store backends

Otto reads credentials through a **creds store**: a small class that answers
"which logins does the machine with this inventory key accept?" with the same
`creds` entries a lab file spells. One ships with otto, the `json` file
({ref}`credentials-layered`); anything else — a vault, a secrets manager, a
team database — is a class you register from your own repo.

This page is the contract. The user-facing side — the `[creds]` table, the
three-layer merge, what the doctor checks — lives in
{doc}`../guide/configuration/inventory`.

## The protocol

A store satisfies {class}`~otto.creds.protocol.CredsStore`: one attribute and
three methods, all read-only. Otto never writes to a store.

| Member | Contract |
| ------ | -------- |
| `label` | Non-empty string naming this store in provenance and errors, e.g. `json:/home/me/lab_data/creds.json`. An individual lookup failure is instead prefixed by the *file or source path* itself, so a reader can find the file without decoding the label — the built-in `json` store's errors all read `<path>: <reason>`. |
| `lookup(key)` | A `list` of {class}`~otto.models.host.CredSpec` for the inventory key — **`[]` when the store holds none for it**, never `None`, never a raise. Raise {class}`~otto.creds.errors.CredsError` only when the store cannot answer at all. Logins must be unique within one key; the list must be equal on a second call. |
| `list_keys()` | Every key this store holds, sorted — or **`None`** if the store cannot enumerate (a vault that hands out a secret by name but lists nothing). Never an empty list standing in for "cannot say": the doctor's orphan check reads `[]` as "no orphans". Never raises: "cannot say" is `None`, not a `CredsError` — the conformance helper counts a raise here as a violation. |
| `fingerprint()` | A value that changes whenever the entries may have; `None` means "not cacheable". Folded into the inventory's own fingerprint, so a rotated password invalidates shell completion the way an edited inventory does. Never raises: an unreachable backend reports `None`, the same as "not cacheable" — the conformance helper counts a raise here as a violation too. |

Optionally implement {class}`~otto.inventory.protocol.SupportsStatPaths` —
`stat_paths()` returning the files your fingerprint is derived from — and the
completion shim revalidates your store by `os.stat` alone, and the doctor
checks those files' modes.

Two rules that are easy to miss:

- **Construction does no I/O.** No socket, no file read, no environment
  variable that must be set. The first `lookup` does the work, so a lab with
  no referenced entry never touches the store.
- **Return whole entries, not passwords.** An entry may carry a route
  (`proxy`, `via`, `params`) if your source of record holds one, but it need
  not: the lab file adds routes on top ({ref}`credentials-layered`), and a
  store that knows only logins and passwords is the common case.

```python
from otto.models.host import CredSpec


class MyVaultStore:
    def __init__(self, repo_dir=None, *, url, mount="lab"):
        self.url = url.rstrip("/")
        self.mount = mount
        self.label = f"vault:{self.url}/{mount}"

    def lookup(self, key):
        secret = self._client().read(f"{self.mount}/{key}")  # first I/O happens here
        if secret is None:
            return []
        return [CredSpec(login=login, password=pw) for login, pw in secret.items()]

    def list_keys(self):
        return None  # this vault cannot enumerate

    def fingerprint(self):
        return None
```

## Selecting it in settings

Register the store under a bare name from an `init` module, then select it:

```python
# my_creds.py  (listed in init = [...])
from otto.creds import register_creds_backend
from my_company.vault import MyVaultStore

register_creds_backend("vault", MyVaultStore)
```

```toml
[creds]
backend = "vault"
url = "https://vault.example"
mount = "lab"
```

Otto constructs it as `MyVaultStore(repo_dir=<declaring directory>,
url="https://vault.example", mount="lab")` — every key in the table except
`backend` becomes a keyword argument, and `repo_dir` is the directory the
declaration came from (the repository root for a project table, `~/.otto`
for the user file). Reject a kwarg you do not understand with a `TypeError`
or `ValueError`: otto wraps it into an error naming the settings file and the
backend.

{func}`~otto.creds.register_creds_backend` refuses a duplicate name unless
you pass `overwrite=True`; {func}`~otto.creds.get_creds_backend_class`
resolves a name. This is the same named-registry mechanism otto uses for
inventories, host sources and reservation backends — see
{doc}`Extension points <../architecture/subsystems/extension-points>`.

## Proving it

```python
from otto.testing import assert_creds_store_conforms


def test_my_store_conforms():
    assert_creds_store_conforms(MyVaultStore(url="https://vault.example"), known_key="carrot-b1")
```

{func}`~otto.testing.assert_creds_store_conforms` runs every structural rule
above and, with `known_key`, the behavioural ones, and raises once listing
every violation.
