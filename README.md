# dotfiles

```sh
git clone git@github.com:lausek/dotfiles.git
dotfiles/install
```

`install` links the configs into place and installs the tools (needs [uv](https://docs.astral.sh/uv/)).
It is idempotent, rerun it after every pull. Files in the way are moved to `<path>.backup-<timestamp>`.

To add a config, add a `link <path in repo> <target>` line to `install`.
