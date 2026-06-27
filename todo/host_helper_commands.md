
# Host Ergonomic Improvements

## Host helper functions

There are many common operations that are needed for hosts. The following things can happen on every kind of host:

* `reboot()`
  * default definition for UnixHost is whatever the most universally used reboot mechnanism is. If root is required and the current user is not root, then sudo might be needed.
* `power(state=None)`
  * state can be "on" or "off". default to toggle.
  * I'm unsure of whether powering on can be done via integration tests.
* `install()`
  * `stage_only` option that places products on the host, but does not install them. False by default.
  * Calls `stage()` and only continues on if stag_only is False.
  * Remaining method body is not implemented by default and should be defined by the project.
* `stage()`
  * Transfer/load products onto the host
  * By default, not implemented. Each project defines how the product(s) are transferred to the host, installed, and started.
* `uninstall()`
  * By default, not implemented. Each project defines how the product(s) are uninstalled.
* `is_installed()`
  * Determines whether the products are currently installed.
  * By default, not implemented. Each project defines how the product(s) are uninstalled.
* `is_uninstalled()`
  * default to inverse of `is_installed()`

### Unix helper functions

* `switch_user(user='')`
  * Use the `su` command to switch users.
  * By default, do not specify the user. This results in switching to the root user
  * This could require the root password, so using the regex matching capabilities of commands will be useful.
* `run()` gains the `sudo` flag
  * Determines whether to run `sudo` as a prefix for the command
  * This could require the current user's password, so using the regex matching capabilities of commands will be useful.
