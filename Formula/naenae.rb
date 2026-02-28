# ── Homebrew tap workflow ──────────────────────────────────────────────────────
#
# IMPORTANT: This file is a REFERENCE COPY only.
# Homebrew taps must live in a SEPARATE GitHub repo named "homebrew-naenae":
#   https://github.com/gpxl/homebrew-naenae
#
# To publish a new release:
#   1. Tag the release in this repo:
#        git tag vX.Y.Z && git push origin vX.Y.Z
#
#   2. Compute the release archive sha256:
#        curl -L https://github.com/gpxl/naenae/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
#
#   3. Compute sha256 for PyPI resources:
#        pip download --no-deps --dest /tmp rumps==0.4.0 pyyaml==6.0.2
#        shasum -a 256 /tmp/*.tar.gz /tmp/*.whl
#
#   4. Copy this file to Formula/naenae.rb in github.com/gpxl/homebrew-naenae,
#      filling in all FILL_IN sha256 values.
#
#   5. Users install with:
#        brew tap gpxl/naenae
#        brew install naenae
#
# ──────────────────────────────────────────────────────────────────────────────

class Naenae < Formula
  include Language::Python::Virtualenv

  desc "Claude Max Capacity Monitor — macOS menu bar app"
  homepage "https://github.com/gpxl/naenae"
  url "https://github.com/gpxl/naenae/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "FILL_IN_AFTER_FIRST_RELEASE"
  license "MIT"

  # For development installs before a tagged release:
  #   brew install --HEAD gpxl/naenae/naenae
  head "https://github.com/gpxl/naenae.git", branch: "main"

  depends_on "python@3.11"
  depends_on :macos

  resource "rumps" do
    url "https://files.pythonhosted.org/packages/source/r/rumps/rumps-0.4.0.tar.gz"
    sha256 "FILL_IN"  # pip download --no-deps rumps==0.4.0 && shasum -a 256 rumps-0.4.0.tar.gz
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/P/PyYAML/PyYAML-6.0.2.tar.gz"
    sha256 "FILL_IN"  # pip download --no-deps pyyaml==6.0.2 && shasum -a 256 PyYAML-6.0.2.tar.gz
  end

  def install
    virtualenv_install_with_resources

    # Ship the config template so users can bootstrap their config
    (share/"naenae").install "config.yaml.template"
  end

  service do
    run opt_bin/"naenae"
    keep_alive true
    log_path var/"log/naenae.log"
    error_log_path var/"log/naenae.log"
  end

  def post_install
    config_file = Pathname.new(Dir.home) / ".naenae" / "config.yaml"
    return if config_file.exist?

    (Pathname.new(Dir.home) / ".naenae").mkpath
    config_file.write (share/"naenae/config.yaml.template").read
    opoo "Created #{config_file} — edit it to set your project paths."
  end

  def caveats
    <<~EOS
      Edit your config before starting:
        open ~/.naenae/config.yaml

      Start the menu bar app:
        brew services start naenae

      Or run once in the foreground:
        naenae
    EOS
  end

  test do
    system bin/"naenae", "--help"
  end
end
