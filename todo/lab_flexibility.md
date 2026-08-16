# Lab Definition Philosophy and Combinations

## Issue

Parts of the lab data is truly global and practically set in stone due to them being physical devices. The database can change, but when it does, it's a global truth - all teams everywhere at that current moment in time should be served the same host data. These kinds of lab host entries are likely to be in a database or some globally referenced json file if a database is not stood up.

However, there are times that host entries are virtual and closer to being owned by the product repo. My reasoning is that VMs can change easily - they can be quickly deployed at larger scale, re-imaged, reconfigured, etc. Each project's *team* really controls that defintion, and it can change over time as their emulation needs change. QEMU hosts are another good examplep of host images possibly needing to be cheap to configure and deploy.

If a virtual host is deemed to be globally defined, it could live in the global database. Otherwise, it could go in the project's repo. So the global database(s) can still hold virtual hosts - this just gives projects more flexibility to have project-defined hosts as well.

## Questions

* How do we combine these two needs?
  * Do we have multiple lab "database" entries?
  * Read in the global database, and then consume repo databases/json files?
* What do we do about collisions?
  * Possibly just fail loudly and refuse to go on?

## My Thoughts

I'd like for this to be a general mechanism. An arbitrary list of host data sources can be defined by each repo - databases (either by IP, DNS, or file path location) and json files (either absolute paths or relative to the repo root).
