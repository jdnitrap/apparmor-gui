{
  description = "A GNOME window for AppArmor, as a NixOS module";

  outputs = { self }: {
    nixosModules.default = ./module.nix;
  };
}
