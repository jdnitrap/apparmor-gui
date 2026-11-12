{
  description = "An AppArmor window as a NixOS module";

  outputs = { self }: {
    nixosModules.default = ./module.nix;
  };
}
