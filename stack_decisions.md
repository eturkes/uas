# Stack Decisions

## Container Engine: Podman 5.7.1

**Discovered via:** Dynamic `PATH` inspection on the host system (openSUSE Aeon).

Podman was selected because it is the container engine installed and functional on the host. It was detected first in the discovery order (podman → docker → nerdctl). Podman runs rootless by default, which aligns well with a security-focused sandbox. It is OCI-compliant and uses the same image and Containerfile format as Docker, ensuring portability.

## Base Image: python:3.12-slim

Python 3.12 on Debian slim provides a minimal footprint (~150 MB) while including the full CPython runtime. Python was chosen for three reasons:

1. **Headless browser automation** — Playwright's Python bindings are mature and well-maintained.
2. **Data science readiness** — The Python ecosystem (NumPy, pandas, scikit-learn) can be layered on top for future workloads without changing the base image.
3. **General code execution** — Python is the most common language for agent-driven code generation and execution.

The `-slim` variant omits build tools and documentation, reducing image size and attack surface.

## Scripting Language: Python 3.12

See rationale above. Python 3.12 specifically was chosen for its improved error messages, performance optimizations, and long-term support window.

## Headless Browser Library: Playwright (Python)

Playwright was chosen over Selenium + ChromeDriver for the following reasons:

- **Self-contained browser management** — `playwright install chromium` downloads a pinned Chromium build, eliminating version-mismatch issues between browser and driver.
- **Modern API** — Auto-waiting, network interception, and multi-page support are built in.
- **Reliable headless mode** — Playwright's Chromium runs headless without requiring `--no-sandbox` workarounds common in containerized Selenium setups.
- **Lighter dependency footprint** — No need for a separate ChromeDriver binary or Java runtime.

## Key Dependencies (System-Level)

The Containerfile installs a minimal set of shared libraries required by Chromium at runtime (`libnss3`, `libgbm1`, `libasound2`, etc.). These are the specific libraries Playwright's bundled Chromium links against. The `--no-install-recommends` flag and post-install cleanup (`rm -rf /var/lib/apt/lists/*`) keep the image lean.

## Output Mapping

The container is stateless. All artifacts (screenshots, DOM snapshots) are written to `/output` inside the container, which is bind-mounted to the host's `./output` directory. The `:Z` SELinux relabel flag is applied for compatibility with SELinux-enabled hosts.
