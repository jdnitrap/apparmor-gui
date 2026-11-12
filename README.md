# apparmor-gui

A window for AppArmor. It lists profiles, running services, and installed applications. From the window you can watch a program in complain mode, enforce a profile, disable it, turn audit on, clean duplicate rules, and learn rules from the log.

The window is GTK 4. GTK draws the window itself on X11 or Wayland, so the same program runs under any desktop or window manager. The module installs GTK with the program. It does not install GNOME, KDE, or a window manager, and the desktop file is not limited to one session. On a machine with no application menu, run `apparmor-gui`.

Start the window with:

```
nix run github:jdnitrap/apparmor-gui
```

Installing the NixOS module also puts the window in the application list and loads its saved profiles after each rebuild. The module uses that system's own package set. `nix run` uses the nixpkgs pin in this flake. The flake does not contain a host configuration, a username, or any sample profiles.

`nix profile install` and `nix run` open the window on a machine that already has AppArmor. The window lists the profiles loaded there, the profiles NixOS installed, the running services, and the installed applications.

Profiles the window saves go in `~/.local/share/apparmor-gui` for the account that opened it. Choose folder on the Profiles page stores them in another directory instead. The NixOS module reads the list of those folders and loads them again whenever AppArmor starts or reloads. The module does not edit your Nix files. A mode change for a profile that already comes from NixOS is stored beside the profile text and applied after the system profile, so a package update still supplies the new profile text.

The window looks at the account that opened it. It is not built for one machine.

## Use it

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  inputs.apparmor-gui.url = "github:jdnitrap/apparmor-gui";

  outputs = { nixpkgs, apparmor-gui, ... }: {
    nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        apparmor-gui.nixosModules.default
        { services.apparmor-gui.enable = true; }
      ];
    };
  };
}
```

`security.apparmor.enable` is turned on unless you set it yourself. The first time AppArmor is enabled, reboot so the kernel option is active.

`services.apparmor-gui.group` defaults to `wheel`. Local members of that group can run the window's privileged commands without a password. Learn from the log uses `run0`, so that step still follows the normal administrator prompt.

## What is stored

```
~/.local/share/apparmor-gui/profiles/<name>.profile
~/.local/share/apparmor-gui/profiles/<name>.mode
~/.local/share/apparmor-gui/overrides/<name>.mode
~/.config/apparmor-gui/directory
```

The last file is one line, the folder chosen in the window. Until a folder is chosen, the home directory above is used. The profile files are ones this window wrote. An override file only records complain, enforce, or disable for a profile that NixOS already installed. Removing an override brings the NixOS mode back. Removing a profile this window wrote does not delete a NixOS profile of the same name. `/var/lib/apparmor-gui/directories` lists the folders the module loads after a rebuild. It does not hold the profiles.

Flatpak applications are listed and are not given an AppArmor profile. AppArmor would only wrap the `flatpak` program.

Watch prefers `/run/current-system/sw` when that path points at the same file. A profile attached to a `/nix/store` path can go stale after a system update, and the window says so when that happens.
