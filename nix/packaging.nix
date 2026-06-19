# Packaging for the AProver web server + a standalone microVM to test it.
#
#   packages.aprover-web        the uvicorn server wrapped with cbmc/gcc on PATH
#   overlays.default            adds `aprover-web` to a pkgs set
#   nixosConfigurations.aprover-vm   a bootable QEMU microVM running the server
#   apps.vm                     `nix run .#vm` -> boots that microVM locally
#
# The Python environment is built from uv.lock via uv2nix, so the VM ships the
# exact dependency set the project is developed against (including tree_sitter_c,
# which is not packaged in nixpkgs, and the fastapi/uvicorn `web` extra).
{
  inputs,
  self,
  lib,
  ...
}:
let
  # Build the `aprover-web` runnable for an arbitrary pkgs set (so it works both
  # in perSystem.packages and inside the guest via the overlay).
  mkAproverWeb =
    pkgs:
    let
      python = pkgs.python312;

      workspace = inputs.uv2nix.lib.workspace.loadWorkspace {
        workspaceRoot = ../.;
      };

      # Prefer binary wheels; avoids most from-source build fixups.
      pyOverlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      pythonSet =
        (pkgs.callPackage inputs.pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              inputs.pyproject-build-systems.overlays.default
              pyOverlay
            ]
          );

      # Enable the project's `web` extra (fastapi + uvicorn) on top of the
      # default closure (anthropic, tree-sitter*, pydantic, rich, the local
      # aprover/bmc_agent packages).
      venv = pythonSet.mkVirtualEnv "aprover-web-env" (
        workspace.deps.default // { aprover = [ "web" ]; }
      );

      # `web/` is intentionally not part of the wheel (pyproject packages =
      # aprover, bmc_agent), so ship it + brand assets as source on PYTHONPATH.
      # web/server.py resolves static/ and ../assets/ relative to its own path.
      srcRoot = lib.fileset.toSource {
        root = ../.;
        fileset = lib.fileset.unions [
          ../web
          ../assets
        ];
      };
    in
    pkgs.writeShellApplication {
      name = "aprover-web";
      runtimeInputs = [
        venv
        pkgs.cbmc
        pkgs.gcc
        pkgs.binutils
      ];
      text = ''
        export PYTHONPATH="${srcRoot}''${PYTHONPATH:+:$PYTHONPATH}"
        cd "${srcRoot}"
        exec uvicorn web.server:app --host "''${HOST:-0.0.0.0}" --port "''${PORT:-7860}"
      '';
    };
in
{
  flake.overlays.default = final: _prev: {
    aprover-web = mkAproverWeb final;
  };

  # Importable host service: `services.aprover`.
  flake.nixosModules.default = import ./nixos-module.nix { inherit self inputs; };
  flake.nixosModules.aprover = self.nixosModules.default;

  # Standalone microVM that just runs the server (default port 7860). Handy for
  # `nix run .#vm` and `nixos-rebuild build-vm`-style local testing.
  flake.nixosConfigurations.aprover-vm = inputs.nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      inputs.microvm.nixosModules.microvm
      { nixpkgs.overlays = [ self.overlays.default ]; }
      (import ./guest.nix { })
    ];
  };

  perSystem =
    { pkgs, system, ... }:
    {
      packages.aprover-web = mkAproverWeb pkgs;

      # The standalone VM is x86_64-linux; only expose `nix run .#vm` there.
      apps = lib.optionalAttrs (system == "x86_64-linux") {
        vm = {
          type = "app";
          program = "${self.nixosConfigurations.aprover-vm.config.microvm.declaredRunner}/bin/microvm-run";
        };
      };
    };
}
