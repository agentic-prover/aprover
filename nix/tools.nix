# From-source builds of the two model checkers that nixpkgs does not ship in a
# usable form, exposed as `pkgs.jbmc` and `pkgs.kani` via `overlays.tools`:
#
#   jbmc  — nixpkgs builds CBMC with -DWITH_JBMC=OFF, so we rebuild the same
#           CBMC source (with submodules) WITH_JBMC=ON. The Java models library
#           is compiled by Maven under JDK 8 (the pom hard-codes
#           ${java.home}/lib/rt.jar), against a pre-fetched offline repo.
#
#   kani  — not in nixpkgs at all. Built from source against the exact nightly
#           it pins (rust-toolchain.toml) supplied by fenix, then assembled into
#           the layout kani-driver expects for an `InstallType::Release` install
#           (<base>/bin, <base>/lib, <base>/toolchain), with `kani`/`cargo-kani`
#           wrappers that dispatch by argv0 and put the solver backends on PATH.
#
# `packaging.nix` composes `overlays.tools` into `overlays.default`; the devshell
# and the aprover-web runtime consume it via `pkgs.extend self.overlays.tools`.
{ inputs, ... }:
let
  toolsOverlay =
    final: _prev:
    let
      inherit (final) lib;

      ##########################################################################
      # JBMC — CBMC built WITH_JBMC=ON
      ##########################################################################

      # Same tag as nixpkgs' cbmc (6.9.0) but WITH submodules, so
      # jbmc/lib/java-models-library is present.
      cbmcSrc = final.fetchFromGitHub {
        owner = "diffblue";
        repo = "cbmc";
        tag = "cbmc-6.9.0";
        fetchSubmodules = true;
        hash = "sha256-ydA8nPwg/Hbuwvw8fTpy5P8KgNGVt04gnSncoBQ3eTw=";
      };

      # The java-models-library pom compiles with <source>1.8</source> and refers
      # to ${java.home}/lib/rt.jar, which only exists in a JDK 8. Maven itself
      # therefore has to run on JDK 8 so ${java.home} resolves correctly.
      maven8 = final.maven.override { jdk_headless = final.jdk8_headless; };

      # Fixed-output derivation: the one place the jbmc build is allowed network
      # access. Pre-populates an offline Maven repository with exactly what
      # `mvn -Dmaven.test.skip=true package` of the models library needs
      # (including org.cprover.util:cprover-api:1.0.0 from Maven Central).
      jbmcMavenDeps = final.stdenv.mkDerivation {
        pname = "jbmc-maven-deps";
        version = "6.9.0";
        src = cbmcSrc;
        nativeBuildInputs = [ maven8 ];
        dontConfigure = true;
        buildPhase = ''
          runHook preBuild
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          mvn -f jbmc/lib/java-models-library/pom.xml \
            -Dmaven.repo.local="$out" -Dmaven.test.skip=true package
          runHook postBuild
        '';
        installPhase = ''
          runHook preInstall
          # Drop non-reproducible bookkeeping so the FOD hash is stable
          # (mirrors nixpkgs' maven.buildMavenPackage fetched-deps cleanup).
          find "$out" -type f \
            \( -name '_remote.repositories' \
               -o -name 'resolver-status.properties' \
               -o -name 'maven-metadata-*.xml' \
               -o -name '*.lastUpdated' \) -delete
          runHook postInstall
        '';
        dontFixup = true;
        outputHashMode = "recursive";
        outputHashAlgo = "sha256";
        outputHash = "sha256-Swc+MSIRxsv+c1p71h/BDWOwMiZU6mt8p9Fv+xtcUIo=";
      };

      jbmc = final.cbmc.overrideAttrs (old: {
        pname = "jbmc";
        src = cbmcSrc;
        nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [
          maven8
          final.jdk8_headless
        ];
        cmakeFlags = [
          "-DWITH_JBMC=ON"
          "-Dsat_impl=cadical"
        ];
        postPatch = (old.postPatch or "") + ''
          # We only need the jbmc binary + core-models.jar. The `java-regression`
          # block maven-builds hundreds of test projects (more deps, more
          # network). jbmc/src/jbmc/CMakeLists.txt does
          # `add_dependencies(jbmc java-regression)`, so the target must still
          # exist — replace it with an empty target that just pulls in
          # java-models-library (which jbmc genuinely needs). It is the last
          # thing in the file, so truncating from its comment is safe.
          sed -i '/# java regression tests/,$d' jbmc/CMakeLists.txt
          cat >> jbmc/CMakeLists.txt <<'CMK'
          add_custom_target(java-regression)
          add_dependencies(java-regression java-models-library)
          CMK

          # Drive the models-library Maven build fully offline against the
          # pre-fetched repo. A project-level .mvn/maven.config is picked up
          # regardless of HOME/env, so it doesn't depend on phase env
          # propagation. `mvn package` only reads from the local repo, so the
          # read-only store path is fine.
          mkdir -p jbmc/lib/java-models-library/.mvn
          cat > jbmc/lib/java-models-library/.mvn/maven.config <<CFG
          --offline
          --batch-mode
          -Dmaven.repo.local=${jbmcMavenDeps}
          CFG
        '';
        # The WITH_JBMC build also produces cbmc/goto-*/cprover and share/cbmc,
        # which collide with the separate nixpkgs `cbmc` package in a shared env
        # (devshell, aprover-web). Keep only the jbmc binary; it resolves
        # core-models.jar / cprover-api.jar relative to its own bin/ in $out/lib.
        postInstall = (old.postInstall or "") + ''
          find "$out/bin" -mindepth 1 ! -name jbmc -delete
          rm -rf "$out/share" "$out/include"
        '';
        # versionCheckHook in the base derivation runs `cbmc --version`, which we
        # have just removed; the jbmc binary is smoke-tested separately.
        doInstallCheck = false;
        meta = (old.meta or { }) // {
          description = "Java Bounded Model Checker (CBMC built with WITH_JBMC=ON)";
          mainProgram = "jbmc";
        };
      });

      ##########################################################################
      # Kani — Rust Bounded Model Checker, built from source
      ##########################################################################

      fenix = inputs.fenix.packages.${final.stdenv.hostPlatform.system};

      kaniSrc = final.fetchFromGitHub {
        owner = "model-checking";
        repo = "kani";
        tag = "kani-0.67.0";
        fetchSubmodules = true;
        hash = "sha256-Advfh0BWvvEbnwWvTpHzu/7MI9P0/dhzvtX9r2qnXeI=";
      };

      # Exact toolchain from kani's rust-toolchain.toml:
      #   channel = nightly-2025-11-21
      #   components = llvm-tools, rustc-dev, rust-src, rustfmt
      # kani-compiler links rustc_private, hence rustc-dev + rust-src; -Z
      # build-std (used to compile the Kani sysroot libs) also needs rust-src.
      kaniToolchain =
        (fenix.toolchainOf {
          channel = "nightly";
          date = "2025-11-21";
          sha256 = "sha256-P39FCgpfDT04989+ZTNEdM/k/AE869JKSB4qjatYTSs=";
        }).withComponents
          [
            "cargo"
            "rustc"
            "rust-std"
            "rust-src"
            "rustc-dev"
            "llvm-tools-preview"
            "rustfmt"
            "clippy"
          ];

      # Solver backends kani-driver shells out to at runtime.
      kaniRuntimePath = lib.makeBinPath [
        final.cbmc
        final.kissat
        final.z3
        final.cvc5
      ];

      kani = final.stdenv.mkDerivation (_finalAttrs: {
        pname = "kani";
        version = "0.67.0";
        src = kaniSrc;

        # Kani builds its sysroot libraries with `cargo -Z build-std`, a nested
        # cargo invocation that pulls in the *standard library's* own
        # dependencies (object, hashbrown, …) at versions from the toolchain's
        # library/Cargo.lock — not kani's workspace lock. Both dependency sets
        # must live in one offline vendor dir. The workspace vendor's
        # .cargo/config.toml (relative `directory = "cargo-vendor-dir"`, plus the
        # git-source entries) is preserved; the std crates are merged alongside
        # so the crates-io replacement resolves them too.
        cargoDeps =
          let
            workspaceVendor = final.rustPlatform.importCargoLock {
              lockFile = "${kaniSrc}/Cargo.lock";
              allowBuiltinFetchGit = true;
            };
            stdVendor = final.rustPlatform.importCargoLock {
              lockFile = "${kaniToolchain}/lib/rustlib/src/rust/library/Cargo.lock";
            };
          in
          # Must be named `cargo-vendor-dir`: importCargoLock's config.toml
          # references it by that relative basename (`directory =
          # "cargo-vendor-dir"`), and cargoSetupHook keys off the name.
          final.runCommand "cargo-vendor-dir" { } ''
            cp -a --no-preserve=mode ${workspaceVendor} $out
            cp -rn ${stdVendor}/. $out/
          '';

        nativeBuildInputs = [
          kaniToolchain
          final.rustPlatform.cargoSetupHook
          final.makeWrapper
          final.cmake
          final.python3
          final.pkg-config
        ];
        buildInputs = [ final.openssl ];

        dontUseCmakeConfigure = true;

        # Kani bakes the toolchain name via env!("RUSTUP_TOOLCHAIN") at compile
        # time (e.g. tools/build-kani, kani-driver's toolchain_shorthand). fenix
        # is not rustup, so set it explicitly to the pinned channel.
        # kani-compiler/build.rs also reads RUSTUP_HOME.unwrap() to build a dev
        # rpath; only its presence matters here — the real runtime rpath is the
        # relative `$ORIGIN/../toolchain/lib` it also emits, which resolves to
        # our <base>/toolchain/lib symlink (and the wrappers also set
        # LD_LIBRARY_PATH as a backstop).
        RUSTUP_TOOLCHAIN = "nightly-2025-11-21";
        RUSTUP_HOME = "/build/.rustup-unused";

        # `cargo build-dev -- --release` builds the binaries (kani-compiler,
        # kani-driver, kani-cov) and the Kani+std sysroot libraries into
        # target/kani/{bin,lib,playback,no_core} via -Z build-std.
        buildPhase = ''
          runHook preBuild
          export CARGO_NET_OFFLINE=true
          export RUSTC_BOOTSTRAP=1
          cargo build-dev -- --release
          runHook postBuild
        '';

        # Assemble the InstallType::Release layout kani-driver detects at runtime
        # via current_exe(): <base>/bin/{kani-driver,kani-compiler}, <base>/lib,
        # <base>/toolchain (for `cargo`), <base>/library (for kani_lib.c).
        installPhase = ''
          runHook preInstall
          base="$out/lib/kani"
          mkdir -p "$base" "$out/bin"

          cp -r target/kani/* "$base/"
          cp -r library "$base/library"
          cp -r scripts "$base/scripts"

          # Rust toolchain that kani-driver expects under <base>/toolchain.
          ln -s ${kaniToolchain} "$base/toolchain"

          # `kani` and `cargo-kani` both exec the one driver binary; kani-driver
          # dispatches on argv0, and resolves its sysroot from current_exe().
          for name in kani cargo-kani; do
            makeWrapper "$base/bin/kani-driver" "$out/bin/$name" \
              --argv0 "$name" \
              --prefix PATH : "${kaniRuntimePath}" \
              --prefix LD_LIBRARY_PATH : "${kaniToolchain}/lib"
          done
          runHook postInstall
        '';

        dontFixup = false;

        meta = {
          description = "Kani Rust Verifier (bounded model checker for Rust)";
          homepage = "https://github.com/model-checking/kani";
          license = with lib.licenses; [
            mit
            asl20
          ];
          mainProgram = "kani";
          platforms = lib.platforms.linux;
        };
      });
    in
    {
      inherit jbmc kani;
    };
in
{
  flake.overlays.tools = toolsOverlay;

  perSystem =
    { pkgs, ... }:
    let
      toolsPkgs = pkgs.extend toolsOverlay;
    in
    {
      packages.jbmc = toolsPkgs.jbmc;
      packages.kani = toolsPkgs.kani;
    };
}
