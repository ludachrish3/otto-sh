
# Docker Improvements

At a minimum more thorough documentation is needed in the user guide for how to define and manage docker images and containers for each project. Additionally, I'd like to introduce a desired workflow for defining Docker images and containers per project, and how they'd compose together.

## Per-project

### Files

Each project will have a Dockerfile per service and a number of compose files - one per use-case. I'm thinking that these items can be configured in the projects' settings.toml file.

### Usage

The scope of the `otto docker` CLI command (and the analagous library that supports it) is to manage the dockerfiles and compose files. Compose files often have lab-specific or runtime dynamic values that need pre-processing before getting shipping over to the desired host. There are also shell environment variables needed when executing the compose command. My thought is that some kind of repo hook should be defined to modify a temp copy of the template compose file (if necessary), ship the modified compose file and the dockerfile to the target host, build using the dockerfile, compose using the modified compose file that was shipped, and that's it.

For multiple projects, each one follows this cycle of local compose file modification, and each docker image is built sequentially (in dependency order) and then a single compose command that includes all compose files would be run at once. The environment variables for all projects would be made available for the single compose command.

This docker work would be nice to be registered per use-case so that library code can effectively say "deploy this use-case" and then do further setup that's outside of docker's scope.

### Questions

- Is this a fairly standard way of managing docker images and containers? Does the compose usage here follow typical guidelines?
- Do we want the project to define which host the services get deployed on? Is that too rigid? Is there possibly a better way to be more flexible so that some kind of project-specific logic can determine which host gets the services deployed?
- Is the multi-project coordinator the right place to put this multi-project container orchestration?
- How should the CLI and the python library change to support this kind of usage paradigm? Is is already covered?
- Is the `otto docker <verb> --on` syntax really needed? Would it just change which host this is done on?
  - I tried to demo otto's docker features, and using `up` and `down` via the `--on` argument did not work. It's possible I just did it wrong, but I'd also like for you to do some testing to make sure that running `otto docker up` and `otto docker down` really works
