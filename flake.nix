{
  description = "An AppArmor window as a NixOS module";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      each = nixpkgs.lib.genAttrs systems;
    in
    {
      nixosModules.default = ./module.nix;

      packages = each (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.callPackage ./package.nix { };
        }
      );

      apps = each (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/apparmor-gui";
        };
      });
    };
}
