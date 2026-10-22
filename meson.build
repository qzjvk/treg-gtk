project(
  'treg-console',
  version: '1.0.0',
  license: 'MIT',
  meson_version: '>=1.8.0',
)

# The python running meson (a venv's python when you build from a venv, the
# build venv's python under meson-python). Its purelib is where the package
# goes: lib/python3.X/site-packages under the install prefix.
py = import('python').find_installation()

pkgdir = py.get_install_dir(pure: true) / 'treg_console'

install_data(
  'treg_console/__init__.py',
  'treg_console/__main__.py',
  'treg_console/app.py',
  'treg_console/client.py',
  'treg_console/desktop.py',
  'treg_console/icon.png',
  install_dir: pkgdir,
)

# the `treg-console` command: a small launcher script (uses python3 from PATH)
install_data(
  'data/treg-console.in',
  rename: 'treg-console',
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# desktop integration, installed system-wide (the app also writes a user-level
# copy at runtime, so running from a checkout works too)
install_data(
  'data/treg.console.desktop',
  install_dir: get_option('datadir') / 'applications',
)
install_data(
  'treg_console/icon.png',
  rename: 'treg-console.png',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / '256x256' / 'apps',
)
install_data(
  'treg_console/icon.png',
  rename: 'treg-console.png',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / '512x512' / 'apps',
)