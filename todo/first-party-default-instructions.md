# Default Common Instructions

## Issue

There are extremely common instructions that basically every project needs. Things like:

* Installing a lab
* Uninstalling a lab
* Checking whether a lab is installed
* Cleaning up all remnants from a lab (products and dev tools)
* Gathering logs (product/debug/all)
* Installing tools
  * Dev tools (ones defined in the repo, similar to products. They should mirror the `Product` objects that currently exist, but be focused on internal tools for each board)
  * Toolchain tools (defined with the toolchain, like gdb, strace, etc.)

There's a lot of symmetry here with existing host methods and possibly host methods that should be added. Currently, I don't believe there's any kind of log retrieval mechanism, for example.

Additionally, once a project-level install, uninstall, is_installed, is_uninstaled, and is_clean are all defined, providing test fixtures that can ensure an installed, uninstalled, or clean state for each test case would be extremely useful. It's very common that different test cases need to ensure a specific start state, and making this fixture available for free would be a BOON for testing.

## Questions

* How do we construct these default instructions?
  * My initial thought is that we should just iterate through all hosts in a lab and run their boards' related method of the same name (install calls all boards' `install()` method)?
* Can we use project dependency order to determine the project installation sequence?
* How should a project define an override for an instruction?
  * It's very likely that a project doesn't just simply iterate through all boards in any happenstance order. They very possibly need to install hosts in a specific order or via specific steps that are outside of strictly the board instalation procedures. The override very possibly will eventually loop through some hosts and use their `install()` method, but there could be other things to do in addition.
* How to handle instruction options?
  * The default instruction options might be good enough, but specific boards or even the project might need to provide additional options. How do we want to handle this patchwork combination of options coming together? A goal of mine is to always make the default as cheap and simple as possible so that the otto users have something that could work with as little effort as possible. I just don't want a single additional option on 1 host out of the rest to cause an issue because it's very possible that only a subset of the hosts even need certain options to apply to them.
  * It could be that this is too complex to tackle at this time and we get the basics working first and then revisit the combination of per-board options separately?

## My Thoughts

One of the first things we need to do is define all of the default methods for hosts that do not currently exist:

* `get_logs(product=True, debug=True)` - Wrapper that conditionally gathers product and debug logs. By default, all logs are retrieved from the host. The directory tree for these logs must be well-defined, documented, and tested. An approach could be to mirror the coverage file directory structure - each host's ID is used to differentiate the collection of files from the other hosts. NOTE: I don't think it's considered a failure to retrieve zero logs, but it is possible that we'll want to codify an expectation that *some* logs should be retrieved. I think by default we should not require a nonzero number of logs, but have an option to require product logs. I don't see a strong case for requiring debug logs other than for symmetry. It's very possible users would never require debug logs, but there's no harm in adding the option.
  * `get_product_logs()` - Gets just product log files from the host. NOTE: the logic does not necessarily run on the host. Sometimes an external retrieval mechanism must be used
  * `get_debug_logs()` - Gets just debug log files from the host. NOTE: the logic does not necessarily run on the host. Sometimes an external retrieval mechanism must be used
t
* `install_tools(dev=True, toolchain=False)` - Wrapper that conditionally installs all kinds of tools on a host. Toolchain tools are turned off by default because they're so large and time consuming to transfer and are infrequently used. Development tools are often much smaller and needed more often.
  * `install_dev_tools()` - Installs the repo's defined development tools (the definition of `DevTool`s should be EXTREMELY similar to how repo `Products` are defined)
  * `install_toolchain_tools()` - Installs the host's defined toolchain tools and libraries onto the host. These tools should likely be defined in the `Toolchain` object to ensure that all required libraries and executables are defined and have host installation locations known. The steps also often require root privileges to install the tools in root-owned directories, so the installation steps can be complex. If each tool is required to specify an owner, (e.g. 'root', 'sql_user`, etc.) as well as destination directories, then I think that a default method could be defined. The key is that otto users need to be able to override this method because this kind of thing gets complicated quickly.
* `cleanup(get_product_logs=True, get_debug_logs=True)` vs. `uninstall(get_product_logs=True, get_debug_logs=True)` - There's a subtle difference between these methods. Uninstalling is strictly taking step to stop the product from running and remove its presence from the host. Cleanup goes further. It should likely call `uninstall()` first to ensure that the product is no longer present. `uninstall()` is also likely the owner of calling the log retrieval function(s) because it also shares this need. Then it removes all of the other files and artifacts from the host - dev tools, toolchain tools, debug logs, and product logs. Cleanup and uninstall should both get logs by default. A lost set of logs can lead to frustration at a minimum and be a major setback at worst. Dispatching to the plain `get_logs()` is good enough I think. Maybe `cleanup()` and `uninstall()` take a `get_debug_logs` and a `get_product_logs` value and those are both passed to a `get_logs()` call in `uninstall()`?
