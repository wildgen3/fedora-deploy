# Only the desktop comes from the installer (Fedora repos). Everything else is
# installed from the manifests by deploy-system.sh in %post, so there is one
# source of truth and a flaky third-party repo can never fail the install.
%packages
@^kde-desktop-environment
@kde-apps
@kde-media
@firefox
dnf5-plugins
git
curl
pciutils
%end
