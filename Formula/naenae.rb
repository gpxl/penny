# ── Homebrew tap workflow ──────────────────────────────────────────────────────
#
# IMPORTANT: This file is a REFERENCE COPY only.
# The live formula must live in the tap repo:
#   https://github.com/gpxl/homebrew-naenae  →  Formula/naenae.rb
#
# Publishing a new release:
#   1. Tag:  git tag vX.Y.Z && git push origin vX.Y.Z
#   2. Get the archive sha256:
#        curl -L https://github.com/gpxl/naenae/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
#   3. Update `url` + `sha256` in this file, then copy to the tap repo.
#   4. If any dependency version changed, regenerate resource sha256s:
#        brew update-python-resources gpxl/naenae/naenae
#   5. Push the tap repo.
#
# Users install with:
#   brew tap gpxl/naenae
#   brew install naenae
#
# ──────────────────────────────────────────────────────────────────────────────

class Naenae < Formula
  include Language::Python::Virtualenv

  desc "Claude Max usage monitor — macOS menu bar agent"
  homepage "https://github.com/gpxl/naenae"
  url "https://github.com/gpxl/naenae/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "FILL_IN_AFTER_TAGGING"
  license "MIT"

  # Install the latest commit for development:
  #   brew install --HEAD gpxl/naenae/naenae
  head "https://github.com/gpxl/naenae.git", branch: "main"

  depends_on "python@3.12"
  depends_on :macos

  # ── Python dependencies ─────────────────────────────────────────────────────
  # sha256s were computed from:
  #   pip download --no-deps --no-binary :all: --dest /tmp/nr \
  #     "pyobjc-core>=10.0" "pyobjc-framework-Cocoa>=10.0" \
  #     "pyyaml>=6.0" "setproctitle>=1.3" "pexpect>=4.8" "pyte>=0.8" ptyprocess
  #   shasum -a 256 /tmp/nr/*.tar.gz
  #
  # To regenerate after a version bump:
  #   brew update-python-resources gpxl/naenae/naenae

  resource "pyobjc-core" do
    url "https://files.pythonhosted.org/packages/source/p/pyobjc-core/pyobjc_core-12.1.tar.gz"
    sha256 "2bb3903f5387f72422145e1466b3ac3f7f0ef2e9960afa9bcd8961c5cbf8bd21"
  end

  resource "pyobjc-framework-Cocoa" do
    url "https://files.pythonhosted.org/packages/source/p/pyobjc-framework-Cocoa/pyobjc_framework_Cocoa-12.1.tar.gz"
    sha256 "5556c87db95711b985d5efdaaf01c917ddd41d148b1e52a0c66b1a2e2c5c1640"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/P/PyYAML/PyYAML-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "setproctitle" do
    url "https://files.pythonhosted.org/packages/source/s/setproctitle/setproctitle-1.3.7.tar.gz"
    sha256 "bc2bc917691c1537d5b9bca1468437176809c7e11e5694ca79a9ca12345dcb9e"
  end

  resource "pexpect" do
    url "https://files.pythonhosted.org/packages/source/p/pexpect/pexpect-4.9.0.tar.gz"
    sha256 "ee7d41123f3c9911050ea2c2dac107568dc43b2d3b0c7557a33212c398ead30f"
  end

  resource "ptyprocess" do
    url "https://files.pythonhosted.org/packages/source/p/ptyprocess/ptyprocess-0.7.0.tar.gz"
    sha256 "5c5d0a3b48ceee0b48485e0c26037c0acd7d29765ca3fbb5cb3831d347423220"
  end

  resource "pyte" do
    url "https://files.pythonhosted.org/packages/source/p/pyte/pyte-0.8.2.tar.gz"
    sha256 "5af970e843fa96a97149d64e170c984721f20e52227a2f57f0a54207f08f083f"
  end

  # ── Install ─────────────────────────────────────────────────────────────────

  def install
    virtualenv_install_with_resources

    # Ship the config template for post_install and manual bootstrapping.
    (share/"naenae").install "config.yaml.template"
  end

  # ── launchd service ─────────────────────────────────────────────────────────
  # `brew services start naenae` installs a LaunchAgent that starts Nae Nae
  # at login. LaunchAgents run in the user's GUI session — required for
  # AppKit/menu bar apps. No .app bundle or code signing needed.

  service do
    run [opt_bin/"naenae"]
    keep_alive true
    log_path var/"log/naenae.log"
    error_log_path var/"log/naenae.log"
    # Ensure `claude` and `bd` (npm global installs) are on PATH.
    # std_service_path_env provides /opt/homebrew/bin and standard system dirs.
    environment_variables PATH: std_service_path_env
  end

  # ── First-run config ─────────────────────────────────────────────────────────

  def post_install
    config = Pathname.new(Dir.home)/".naenae"/"config.yaml"
    return if config.exist?

    (Pathname.new(Dir.home)/".naenae").mkpath
    config.write (share/"naenae/config.yaml.template").read
    opoo "Config created at #{config} — edit it before starting Nae Nae."
  end

  # ── User-facing notes ────────────────────────────────────────────────────────

  def caveats
    config = Pathname.new(Dir.home)/".naenae"/"config.yaml"
    <<~EOS
      Before starting, set your project paths in the config:
        open #{config}

      Then start the menu bar app (auto-starts at login):
        brew services start naenae

      Or run once in the foreground (useful for debugging):
        naenae

      To stop:
        brew services stop naenae

      Logs:
        tail -f #{var}/log/naenae.log

      Prerequisites — must be in PATH before starting the service:
        claude  →  npm install -g @anthropic-ai/claude-code
        bd      →  npm install -g beads-cli
    EOS
  end

  # ── Formula self-test ────────────────────────────────────────────────────────

  test do
    # Verify the virtualenv can import the package without a display server.
    system libexec/"bin/python", "-c", "import naenae; print('ok')"
  end
end
