# Minato

Minato is the meta handler of the jfalcou libraries, the repository for the work that concerns the
libraries as a set. It holds:

- **Library list**: the libraries minato follows, with the versions each library keeps fixed.
- **Pin updates**: a weekly pass that keeps the libraries current with each other.
- **minato-sendo**: the bot that opens the pull requests of minato.

## Library list

Each library minato follows has a folder in `libraries/`:

- `library.json`: the repository of the library, and whether minato opens pull requests there.
- `holds.json`, where needed: the pins the library keeps fixed, each with its reason.
- `pins.json`, written by minato: every pin of the library, the newest version it can take, and
  the pull request that moves it.

`minato.json` names the owners whose repositories count as libraries, and the images whose tags
count as pins.

## Pin updates

The libraries depend on each other. A library fetches TTS and EVE when it builds, calls the CI
workflows of Copacabana, and runs its jobs in the compilers image. Each of these dependencies is
pinned to a version, which stays behind once its library publishes a newer one.

Every Friday, after each release of Copacabana, and also when started by hand, minato compares each
pin with the newest version of its library. A library whose pins are behind receives a single pull
request that moves all of these pins, and its CI runs against that pull request as against any
other. Pins that a library keeps fixed, and pins that follow a branch, are left as they are.

Dependabot keeps the actions and the pre-commit hooks from outside the set, and leaves the jfalcou
pins to minato.

## minato-sendo

minato-sendo is a GitHub App owned by `jfalcou` and installed in the libraries minato follows. The
pull requests of minato carry its name, and the maintainers of each library review and merge these
pull requests like any other.
