{ inputs, ... }:
{
  imports = [
    inputs.flake-root.flakeModule
    inputs.treefmt-nix.flakeModule
  ];
  perSystem =
    { config, pkgs, ... }:
    {
      treefmt.config = {
        package = pkgs.treefmt;
        inherit (config.flake-root) projectRootFile;

        programs = {
          # Nix
          nixfmt.enable = true;
          nixfmt.package = pkgs.nixfmt; # RFC 166 formatter
          deadnix.enable = true;
          statix.enable = true;
          # Bash
          shellcheck.enable = true;
          # NOTE: deliberately no Python reformatter (black/ruff-format) so treefmt
          # never rewrites the project's Python sources on commit.
        };

        settings.global.excludes = [
          ".git/*"
          ".direnv/*"
          ".envrc"
          "artifacts/*"
          "experiments/*"
          "findings/*"
          "assets/*"
          "presentation/*"
          "*.diff"
        ];
      };

      formatter = config.treefmt.build.wrapper;
    };
}
