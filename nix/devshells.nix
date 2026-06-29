{ inputs, self, ... }:
{
  imports = [ inputs.devshell.flakeModule ];

  perSystem =
    {
      pkgs,
      config,
      system,
      ...
    }:
    let
      # cbmc is in nixpkgs; jbmc + kani are built from source by nix/tools.nix.
      toolsPkgs = pkgs.extend self.overlays.tools;
    in
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

          # Verification oracles + compiler toolchain the pipeline shells out to.
          #   cbmc        — REQUIRED: C bounded model checker (bmc_agent/cbmc.py)
          #   gcc         — dynamic validation, reproducer loop, and the `cc`
          #                 preprocessor (the gcc wrapper also provides `cc`)
          inherit (pkgs)
            cbmc
            gcc
            binutils
            pkg-config
            ;

          #   jbmc        — Java bounded model checker (bmc_agent/jbmc.py)
          #   kani        — Rust bounded model checker (bmc_agent/kani.py)
          # Both are built from source by nix/tools.nix (nixpkgs ships neither in
          # a usable form). See overlays.tools.
          inherit (toolsPkgs)
            jbmc
            kani
            ;

          #   jdk         — `javac`/`java` for the JBMC backend's compile step
          #                 (bmc_agent/config.py:javac_path / java_classpath).
          inherit (pkgs)
            jdk
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
