# A window for the AppArmor tools. GTK 4 draws it on X11 or Wayland.
# The package does not require a desktop environment or a window manager.
# Profiles the window creates are stored in the home directory of the person
# who saved them, or in a folder that person chose. A registry lists those
# folders so they can be loaded after the system's own profiles. The module
# does not edit the user's Nix files and does not name a user or a host.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.apparmor-gui;
  gui = pkgs.callPackage ./package.nix { };

  # Runs after apparmor.service has loaded NixOS profiles and, on reload,
  # after aa-remove-unknown. A bad profile is reported and does not fail boot.
  loader = pkgs.writeShellScript "apparmor-gui-load" ''
    set -u
    parser=${pkgs.apparmor-parser}/bin/apparmor_parser
    basename=${pkgs.coreutils}/bin/basename
    tr=${pkgs.coreutils}/bin/tr
    note() { printf 'apparmor-gui: %s\n' "$*" >&2; }

    load_tree() {
      dir=$1
      [ -d "$dir" ] || return 0
      if [ -d "$dir/profiles" ]; then
        for profile in "$dir/profiles"/*.profile; do
          [ -f "$profile" ] || continue
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
        done
      fi
      if [ -d "$dir/overrides" ]; then
        for modefile in "$dir/overrides"/*.mode; do
          [ -f "$modefile" ] || continue
          key=$($basename "$modefile" .mode)
          if [ -f "$dir/profiles/$key.profile" ]; then
            continue
          fi
          installed=/etc/apparmor.d/$key
          if [ ! -f "$installed" ]; then
            note "no NixOS profile for $key"
            continue
          fi
          mode=$($tr -d '[:space:]' < "$modefile")
          case "$mode" in
            disable) "$parser" -R "$installed" || note "could not unload $key" ;;
            enforce) "$parser" -r "$installed" || note "could not enforce $key" ;;
            complain) "$parser" -r -C "$installed" || note "could not complain $key" ;;
            *) note "ignored unknown mode for $key" ;;
          esac
        done
      fi
    }

    registry=/var/lib/apparmor-gui/directories
    if [ -f "$registry" ]; then
      while IFS= read -r dir || [ -n "$dir" ]; do
        case "$dir" in
          ""|\#*) continue ;;
        esac
        load_tree "$dir"
      done < "$registry"
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
      gui
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
