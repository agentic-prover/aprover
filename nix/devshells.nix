{ inputs, ... }:
{
  imports = [ inputs.devshell.flakeModule ];

  perSystem =
    {
      pkgs,
      config,
      system,
      ...
    }:
    {
      devshells.default = {
        name = "AProver devshell";

        packages = builtins.attrValues {
          # The project is a uv-managed Python package; uv provisions the
          # interpreter and every dependency from pyproject.toml / uv.lock, so we
          # deliberately do NOT pin python3 here.
          inherit (pkgs)
            uv
            ;

          # Verification oracle + compiler toolchain the pipeline shells out to.
          #   cbmc        — REQUIRED: C bounded model checker (bmc_agent/cbmc.py)
          #   gcc         — dynamic validation, reproducer loop, and the `cc`
          #                 preprocessor (the gcc wrapper also provides `cc`)
          inherit (pkgs)
            cbmc
            gcc
            binutils
            pkg-config
            ;

          # General CLI tooling.
          inherit (pkgs)
            git
            ripgrep
            gnumake
            coreutils
            bash
            ;

          # Optional deductive-verification path, used only with `--oracle frama-c`.
          # frama-c -wp-detect picks up the SMT prover (z3) on PATH. alt-ergo is
          # intentionally omitted — it is unfree in nixpkgs (ocamlpro_nc license).
          inherit (pkgs)
            frama-c
            z3
            ;

          # Dev tooling.
          inherit (inputs.nix-fast-build.packages.${system}) nix-fast-build;
          treefmt = config.treefmt.build.wrapper;
        };

        commands = [
          {
            name = "aprover-verify";
            help = "Run autonomous verification on a source dir";
            command = "exec uv run bmc-agent autonomous \"$@\"";
          }
        ];
      };
    };
}
