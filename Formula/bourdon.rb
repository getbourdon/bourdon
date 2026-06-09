class Bourdon < Formula
  include Language::Python::Virtualenv

  desc "Recognition-first runtime + agent federation memory for human-AI collaboration"
  homepage "https://bourdon.ai"
  url "https://github.com/getbourdon/bourdon/archive/refs/tags/v0.9.0.tar.gz"
  sha256 "d1cc8f999dd6328cf5b839d5371baa74d06e78a489f62a0a3a9e56a9eb71a968"
  license "BUSL-1.1"
  head "https://github.com/getbourdon/bourdon.git", branch: "main"

  depends_on "python@3.12"
  depends_on "rsync" # required for `bourdon sync push/pull`

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # `bourdon --help` must succeed and list the top-level subcommands the
    # quickstart points at. If any of these disappear, the formula's
    # advertised UX drifts from reality.
    help = shell_output("#{bin}/bourdon --help")
    assert_match "setup", help
    assert_match "demo", help
    assert_match "doctor", help
    assert_match "sync", help

    # `bourdon demo --no-keep` must run end-to-end. This exercises the
    # federation pipeline (synthetic data, real code path) -- the most
    # honest smoke test we can run inside a sandboxed brew test environment.
    demo_output = shell_output("#{bin}/bourdon demo --no-keep")
    assert_match "Bourdon cross-machine recognition demo", demo_output
    assert_match "DemoProject", demo_output
  end
end
