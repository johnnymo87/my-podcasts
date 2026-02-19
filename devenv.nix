{ pkgs, ... }:
{
  languages.python = {
    enable = true;
    package = pkgs.python314;
    uv = {
      enable = true;
      sync.enable = true;
    };
  };

  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_22;
    npm.enable = true;
  };

  packages = [
    pkgs.ffmpeg
    pkgs.wrangler
    pkgs.sqlite
  ];

  dotenv.enable = true;

  scripts.test.exec = "uv run pytest";
  scripts.worker-dev.exec = "npm --prefix workers/email-ingest run dev";
  scripts.worker-deploy.exec = "npm --prefix workers/email-ingest run deploy";

  enterShell = ''
    echo "my-podcasts dev environment"
    echo "  Python: $(python --version)"
    echo "  UV:     $(uv --version)"
    echo "  Node:   $(node --version)"
    echo "  npm:    $(npm --version)"
    echo ""
    echo "Commands:"
    echo "  uv sync        - Install/update Python dependencies"
    echo "  test           - Run pytest"
    echo "  worker-dev     - Run email worker locally"
    echo "  worker-deploy  - Deploy email worker"
  '';
}
