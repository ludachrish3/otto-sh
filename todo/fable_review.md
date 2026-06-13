
# Fable Review Guidelines

First things first - does something already achieve `otto`'s goals? Is there maybe a tool, like Robot, that achieves most if not all of this and I should just get more familiar with it?

`otto` is currently directionally correct. There are definitely more features I'd like to add and tweak some aspects of it, but it mostly covers the major features I'm looking for in an automation and regression framework. I'd like an expert's opinion on the state of the repo. I'd like to use `otto` in a professional environment where embedded and Linux C programming (user and kernel) are the main focus. There are no users of otto yet, so now is a good time to radically rethink the project and clean as much up as possible before the interface has to start considering backwards compatibility. NO CHANGES ARE TOO BIG OR TOO SMALL TO BRING UP. Things like:

* code quality
* maintainability
* documentation
* speed
* project structure
* test methodology
  * Is the unit/integration/e2e structure sane? It feels a bit messy.
* CI pipeline

There are plenty of ideas of things to add in the `todo` directory. Please feel free to look those over and judge whether they're worth doing and if there are any pros and cons that have not been considered.

And please leverage the learnings from the wiki you're building to think over the gotchas we've run into over the course of otto's development. Were some of those avoidable with a better or different design? Is there a lot of code duplication that is causing maintainability issues? Are some areas just plain brittle? Are there some additional tools or libraries that would help make some parts of otto simpler or more elegant? I know `pydantic` is one I'd really like to use much more instead of dataclasses. Type safety is a common gotcha in my experience, and getting it as close to "for free" as possible would be fantastic.

## Hard Requirements

Below are the hard requirements that `otto` strives to meet. If they are not met, I'd like them to be pointed out, and pros and cons of remediating them.

* Python 3.10+
* Can be used in an air-gapped environment
* Highly extensible by users
  * Users will develop their own compiled products in their own repos (defined by `OTTO_SUT_DIRS`).
  * These SUT directories (separate code repos) will define their own libraries, instructions, and product test suites with `otto` as a dependency
  * These user repos should be able to plugin, extend, and modify all aspects of `otto`'s capabilities
    * Host behavior needs hooks and mechanisms to provide alternative protocols and protocol options globally and to precise hosts
    * Instructions are independently provided by each SUT repo and appear dynamically in the help menu
    * Test suites are built off of a robust unit testing framework to support regression testing products
* Interface consistency is highly valuable
  * Consistency in the host JSON schema
  * Consistency in interface definitions and available options
    * Plugins or methods being named by a separate string or directly by a function name should probably be unified across config options. Having separate mechanisms could likely be confusing to users.

* Future otto extensibility
  * Please try to determine "hot spots" in `otto`'s codebase - areas that churn often or in unison. This could indicate areas that are poorly set up for maintainability or are poorly designed/implemented.

## Goals

* The learning curve should be as small as possible
  * Robust, easy to read documentation should walk users through how to set up and use `otto` for their projects
  * Succinct, intuitive class, method, and function names should reduce confusion and make reaching for documentation mostly for discovery or best practices
* Compare to what other standard tools offer in this area. Are there test regression or automation feature gaps in `otto`?
* Are there any aspects of otto that seem like they could run into a dead-end from a design or extensibility perspective?

## Feature Suggestions?

`otto` currently offers a lot, but I'm sure there are also plenty of things I haven't thought of yet, or are nice quality of life improvements. Things like more convenient host methods for common tasks, CLI subcommands, and different project architectures are all fair game to run by me. Are all of the currently extensible areas conveniently flexible? Things like the host JSON schema, the performance, monitoring interface, test suite registration, project library discovery and init.

## Perspectives

Please consider the following perspectives when reviewing otto:

* How easy and intuitive is it to start a new project from scratch?
* Are the hook registration paradigms consistent enough?
  * If they do deviate, do they deviate for good reason?
* Are the start-up costs of adopting otto minimal?
  * What would a team need to implement to get going?
    * The scheduler comes to mind
    * The hosts database/JSON file
* Does `otto` scale well on an NFS-mounted file system?
  * All testing to date is on a single host running VMs. Are there areas that might break down if file I/O and network latency grow?
