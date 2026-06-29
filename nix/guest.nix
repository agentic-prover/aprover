# Shared microVM *guest* configuration for the AProver web server.
#
# Returns a NixOS module that runs `aprover-web` (the uvicorn server, see
# nix/packaging.nix) inside a QEMU microVM, with QEMU user-mode (SLIRP)
# networking: outbound connectivity to api.anthropic.com plus an inbound
# host->guest TCP forward.
#
# Consumed by:
#   - the standalone test VM  (flake.nixosConfigurations.aprover-vm)
#   - the host service module (services.aprover -> microvm.vms.aprover.config)
#
# The overlay from this flake (pkgs.aprover-web) must be applied by the caller.
#
# `guestPort` is fixed at 7860 (the server's PORT); `hostPort` is what gets
# exposed on the host.
{
  hostPort ? 7860,
  # Bind the forwarded port to loopback by default — the workbench runs
  # untrusted pasted C and shallow-clones arbitrary URLs server-side, so it
  # shouldn't be reachable from the whole network unless explicitly opted in.
  hostAddress ? "127.0.0.1",
  # Passwordless root on the serial console is a debugging convenience, not
  # something the production service should ship (the guest mounts the host
  # /nix/store and has outbound net). Off unless explicitly enabled.
  debug ? false,
  vcpu ? 2,
  # NB: keep this off exactly 2048 — QEMU hangs at exactly 2 GB of guest RAM
  # (microvm-nix/microvm.nix#171). 4096 also gives the memory-hungry CBMC/JBMC/
  # Kani backends headroom.
  mem ? 4096,
  model ? "claude-sonnet-4-6",
}:
{ lib, pkgs, ... }:
{
  microvm = {
    hypervisor = "qemu";
    inherit vcpu mem;

    # Mount the host's /nix/store over 9p (read-only) instead of building a
    # separate store disk image. This is daemonless (9p is built into QEMU, no
    # virtiofsd) and avoids the erofs store-disk boot path; microvm sets
    # storeOnDisk = false automatically when the store is a share.
    shares = [
      {
        proto = "9p";
        tag = "ro-store";
        source = "/nix/store";
        mountPoint = "/nix/.ro-store";
      }
    ];

    # User-mode networking: no host bridge/tap setup required. Gives the guest
    # outbound internet (for the Anthropic API) and supports forwardPorts.
    interfaces = [
      {
        type = "user";
        id = "aprover";
        mac = "02:00:00:0a:b1:01";
      }
    ];

    # Expose the in-guest server (7860) on the host.
    forwardPorts = [
      {
        from = "host";
        proto = "tcp";
        host.address = hostAddress;
        host.port = hostPort;
        guest.port = 7860;
      }
    ];
  };

  # Auto-login as root on the serial console / tty so the microVM drops
  # straight into a shell without a password prompt (handy for debugging).
  # Gated: the importable production service leaves this off.
  services.getty.autologinUser = lib.mkIf debug "root";

  # DHCP + DNS over the SLIRP link so outbound TLS to api.anthropic.com works.
  systemd.network.enable = true;
  services.resolved.enable = true;

  # Let forwarded connections reach the server.
  networking.firewall.allowedTCPPorts = [ 7860 ];

  systemd.services.aprover-web = {
    description = "AProver web server";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      BMC_AGENT_LLM_MODEL = model;
      BMC_AGENT_LOG_DIR = "/tmp/aprover-logs";
      HOST = "0.0.0.0";
      PORT = "7860";
    };
    serviceConfig = {
      ExecStart = lib.getExe pkgs.aprover-web;
      Restart = "on-failure";
      RestartSec = 2;
      # Isolated unprivileged user with a private writable /tmp for the
      # pipeline's scratch dirs and logs.
      DynamicUser = true;
      PrivateTmp = true;
    };
  };

  system.stateVersion = lib.trivial.release;
}
