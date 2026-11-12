# A GNOME window for the AppArmor tools.
# Profiles the window creates are stored in /var/lib/apparmor-gui and loaded
# after the system's own profiles. The module does not edit the user's Nix files
# and does not name a user, a host, or a configuration path.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.apparmor-gui;

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
    version = "0.1.0";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.wrapGAppsHook4 ];
    buildInputs = [
      pythonEnv
      pkgs.gtk4
      pkgs.libadwaita
      pkgs.gobject-introspection
      pkgs.adwaita-icon-theme
    ];
    installPhase = ''
      mkdir -p $out/bin $out/share/apparmor-gui
      cp ${./src/apparmor-gui.py} $out/share/apparmor-gui/apparmor-gui.py
      cat > $out/bin/apparmor-gui <<EOF
#!/bin/sh
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
    icon = "security-high";
    categories = [ "System" "Security" ];
    startupNotify = true;
  };

  # Runs after apparmor.service has loaded NixOS profiles and, on reload,
  # after aa-remove-unknown. A bad profile is reported and does not fail boot.
  loader = pkgs.writeShellScript "apparmor-gui-load" ''
    set -u
    dir=/var/lib/apparmor-gui
    parser=${pkgs.apparmor-parser}/bin/apparmor_parser
    basename=${pkgs.coreutils}/bin/basename
    tr=${pkgs.coreutils}/bin/tr
    note() { printf 'apparmor-gui: %s\n' "$*" >&2; }

    load_owned() {
      profile=$1
      key=$($basename "$profile" .profile)
      mode=complain
      if [ -f "$dir/profiles/$key.mode" ]; then
        mode=$($tr -d '[:space:]' < "$dir/profiles/$key.mode")
      fi
      case "$mode" in
        disable) "$parser" -R "$profile" || note "could not unload $key" ;;
        enforce) "$parser" -r "$profile" || note "could not enforce $key" ;;
        *) "$parser" -r -C "$profile" || note "could not complain $key" ;;
      esac
    }

    load_override() {
      modefile=$1
      key=$($basename "$modefile" .mode)
      if [ -f "$dir/profiles/$key.profile" ]; then
        return 0
      fi
      installed=/etc/apparmor.d/$key
      if [ ! -f "$installed" ]; then
        note "no NixOS profile for $key"
        return 0
      fi
      mode=$($tr -d '[:space:]' < "$modefile")
      case "$mode" in
        disable) "$parser" -R "$installed" || note "could not unload $key" ;;
        enforce) "$parser" -r "$installed" || note "could not enforce $key" ;;
        complain) "$parser" -r -C "$installed" || note "could not complain $key" ;;
        *) note "ignored unknown mode for $key" ;;
      esac
    }

    if [ -d "$dir/profiles" ]; then
      for profile in "$dir/profiles"/*.profile; do
        [ -f "$profile" ] || continue
        load_owned "$profile"
      done
    fi
    if [ -d "$dir/overrides" ]; then
      for modefile in "$dir/overrides"/*.mode; do
        [ -f "$modefile" ] || continue
        load_override "$modefile"
      done
    fi
    exit 0
  '';
in
{
  options.services.apparmor-gui = {
    enable = lib.mkEnableOption "the AppArmor window and the profile store it keeps across rebuilds";

    group = lib.mkOption {
      type = lib.types.str;
      default = "wheel";
      description = ''
        Local members of this group may run the window's privileged commands
        without a password. Other AppArmor commands, including Learn from the
        log, still use the normal administrator prompt.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.match "[A-Za-z_][A-Za-z0-9_-]*" cfg.group != null;
        message = "services.apparmor-gui.group must be a single group name.";
      }
    ];

    # The window has nothing to show until AppArmor is on. A system that
    # already sets this keeps its own value. The first switch can need a reboot
    # so the kernel parameter is active.
    security.apparmor.enable = lib.mkDefault true;

    environment.systemPackages = [
      app
      backend
      desktop
      pkgs.apparmor-bin-utils
      pkgs.apparmor-utils
      pkgs.apparmor-parser
    ];

    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.policykit.exec" &&
            action.lookup("command") &&
            action.lookup("command").indexOf("apparmor-gui-backend") !== -1 &&
            subject.active && subject.local &&
            subject.isInGroup("${cfg.group}")) {
          return polkit.Result.YES;
        }
      });
    '';

    systemd.tmpfiles.rules = [
      "d /var/lib/apparmor-gui 0755 root root -"
      "d /var/lib/apparmor-gui/profiles 0755 root root -"
      "d /var/lib/apparmor-gui/overrides 0755 root root -"
    ];

    systemd.services.apparmor = lib.mkIf config.security.apparmor.enable {
      serviceConfig.ExecStartPost = lib.mkAfter [ loader ];
      serviceConfig.ExecReload = lib.mkAfter [ loader ];
    };
  };
}
