# The window and its privileged helper. The NixOS module installs this package.
# `nix run` starts the same program.
{ pkgs, lib }:

let
  pythonEnv = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);

  backend = pkgs.writeShellScriptBin "apparmor-gui-backend" ''
    export PATH=${lib.makeBinPath [
      pkgs.apparmor-bin-utils
      pkgs.apparmor-utils
      pkgs.apparmor-parser
      pkgs.systemd
      pkgs.diffutils
      pkgs.coreutils
    ]}
    exec ${pkgs.python3}/bin/python3 -u ${pkgs.writeText "apparmor-gui-backend.py" (builtins.readFile ./src/apparmor-gui-backend.py)} "$@"
  '';

  app = pkgs.stdenv.mkDerivation {
    pname = "apparmor-gui";
    version = "0.2.0";
    dontUnpack = true;
    # The hook only puts GTK's own libraries on the program path.
    nativeBuildInputs = [ pkgs.wrapGAppsHook4 ];
    buildInputs = [
      pythonEnv
      pkgs.gtk4
      pkgs.gobject-introspection
    ];
    installPhase = ''
      mkdir -p $out/bin $out/share/apparmor-gui
      cp ${./src/apparmor-gui.py} $out/share/apparmor-gui/apparmor-gui.py
      cat > $out/bin/apparmor-gui <<EOF
#!/bin/sh
export PATH=${lib.makeBinPath [
        backend
        pkgs.apparmor-bin-utils
        pkgs.apparmor-utils
        pkgs.apparmor-parser
      ]}:\$PATH
exec ${pythonEnv}/bin/python3 $out/share/apparmor-gui/apparmor-gui.py
EOF
      chmod +x $out/bin/apparmor-gui
    '';
  };

  desktop = pkgs.makeDesktopItem {
    name = "apparmor-gui";
    desktopName = "AppArmor";
    comment = "Profiles, services, applications, and AppArmor commands";
    exec = "apparmor-gui";
    categories = [ "System" "Security" ];
    startupNotify = true;
  };
in
pkgs.symlinkJoin {
  name = "apparmor-gui";
  paths = [
    app
    backend
    desktop
  ];
  meta = {
    mainProgram = "apparmor-gui";
    description = "A window for AppArmor";
    platforms = lib.platforms.linux;
  };
}
