# Importable NixOS service: `services.aprover`.
#
# Enabling it declares a QEMU microVM (via microvm.nix) that runs the AProver
# web chat server, with its HTTP port forwarded to the host. The verification
# workload (CBMC + gcc subprocesses, untrusted pasted C) stays isolated in the
# guest.
#
# Bring-your-own-key: the server runs with no API key; visitors paste their own
# Anthropic key in the browser. No host secret is required.
#
# Usage (in a host flake):
#   imports = [ aprover.nixosModules.default ];
#   services.aprover.enable = true;
#   services.aprover.port = 7860;
#
# `self`/`inputs` are this flake's, threaded in so the guest can use its overlay
# and the microvm.nix host module.
{ self, inputs }:
{ config, lib, ... }:
let
  cfg = config.services.aprover;
in
{
  imports = [ inputs.microvm.nixosModules.host ];

  options.services.aprover = {
    enable = lib.mkEnableOption "the AProver web server in a QEMU microVM";

    port = lib.mkOption {
      type = lib.types.port;
      default = 7860;
      description = "Host TCP port to expose the web UI on (forwarded to guest:7860).";
    };

    hostAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      example = "0.0.0.0";
      description = ''
        Host address to bind the forwarded port to. Defaults to "127.0.0.1"
        (local only) — the UI runs untrusted pasted C and clones arbitrary URLs
        server-side, so it shouldn't face the network by default. Set to ""
        (all interfaces) or a specific address to expose it deliberately.
      '';
    };

    debug = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Enable passwordless root auto-login on the guest serial console. A
        debugging convenience only — the guest mounts the host /nix/store and
        has outbound network, so leave this off for any exposed deployment.
      '';
    };

    vcpu = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = "Number of virtual CPUs for the guest.";
    };

    mem = lib.mkOption {
      type = lib.types.ints.positive;
      # Avoid exactly 2048: QEMU hangs at exactly 2 GB of guest RAM
      # (microvm-nix/microvm.nix#171). 4096 also suits the memory-hungry
      # CBMC/JBMC/Kani backends.
      default = 4096;
      description = "Guest RAM in MiB. CBMC is memory-hungry; raise for large inputs.";
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "claude-sonnet-4-6";
      description = "Anthropic model id (BMC_AGENT_LLM_MODEL) for the pipeline.";
    };
  };

  config = lib.mkIf cfg.enable {
    microvm.vms.aprover.config = {
      imports = [
        { nixpkgs.overlays = [ self.overlays.default ]; }
        (import ./guest.nix {
          hostPort = cfg.port;
          inherit (cfg)
            hostAddress
            debug
            vcpu
            mem
            model
            ;
        })
      ];
    };
  };
}
