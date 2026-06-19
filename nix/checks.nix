{ inputs, ... }:
{
  imports = [ inputs.git-hooks-nix.flakeModule ];
  perSystem =
    { config, ... }:
    {
      # Only treefmt (nix + shell). No Python hooks, so committing never
      # reformats the project's Python sources.
      pre-commit = {
        settings = {
          hooks = {
            treefmt = {
              enable = true;
              package = config.treefmt.build.wrapper;
            };
          };
        };
      };
    };
}
