#!/bin/bash
# Copyright (c) 2025, Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#
# MySQL Operator One-Click Build Script
# This script automates the entire build process for MySQL Operator images

set -e  # Exit on error
set -u  # Exit on undefined variable

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PYTHON_VERSION="3.13.9"
PYTHON_TARBALL="Python-${PYTHON_VERSION}.tgz"
PYTHON_DOWNLOAD_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/${PYTHON_TARBALL}"
PYTHON_DEPS_IMAGE="mysql-operator-python-deps:${PYTHON_VERSION}-amd64"
OPERATOR_IMAGE_TAG=${OPERATOR_IMAGE_TAG:-"mysql/mysql-operator"}
BUILD_DIR="/tmp/mysql-operator-build"
CURRENT_DIR="$(pwd)"

# Functions
print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_step() {
    echo -e "${BLUE}▶ $1${NC}"
}

check_dependencies() {
    print_header "Checking Dependencies"

    # Check if Docker is installed
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    print_success "Docker is installed"

    # Check if uv is installed
    if ! command -v uv &> /dev/null; then
        print_warning "uv is not installed. Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if ! command -v uv &> /dev/null; then
            print_error "Failed to install uv. Please install manually."
            exit 1
        fi
    fi
    print_success "uv is installed: $(uv --version)"
}

download_python() {
    print_header "Downloading Python ${PYTHON_VERSION}"

    # Check if tarball already exists
    if [ -f "${PYTHON_TARBALL}" ]; then
        print_warning "Python tarball already exists. Skipping download."
        return
    fi

    print_step "Downloading Python ${PYTHON_VERSION} from ${PYTHON_DOWNLOAD_URL}"
    if wget --no-proxy "${PYTHON_DOWNLOAD_URL}"; then
        print_success "Python tarball downloaded successfully"
    else
        print_error "Failed to download Python tarball"
        print_step "Trying alternative download method..."
        if curl -L -o "${PYTHON_TARBALL}" "${PYTHON_DOWNLOAD_URL}"; then
            print_success "Python tarball downloaded successfully with curl"
        else
            print_error "Failed to download Python tarball with both wget and curl"
            exit 1
        fi
    fi
}

setup_python_environment() {
    print_header "Setting Up Python Environment"

    print_step "Creating build directory: ${BUILD_DIR}"
    rm -rf "${BUILD_DIR}"
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"

    # Create Python virtual environment with uv
    print_step "Creating Python ${PYTHON_VERSION} virtual environment with uv"
    uv venv --python "${PYTHON_VERSION}" || {
        print_error "Failed to create Python ${PYTHON_VERSION} virtual environment"
        print_step "Trying with latest Python 3.13..."
        uv venv --python 3.13
    }
    print_success "Virtual environment created"

    # Install dependencies
    print_step "Installing Python dependencies with uv (using --no-deps for CVE fix)"
    uv pip install -r "${CURRENT_DIR}/docker-deps/requirements.txt" \
        --python "${BUILD_DIR}/.venv/bin/python" \
        --no-deps
    print_success "Dependencies installed"

    # Copy site-packages
    print_step "Copying site-packages to build directory"
    mkdir -p "${BUILD_DIR}/site-packages"
    cp -r "${BUILD_DIR}/.venv/lib/python"*/site-packages/* "${BUILD_DIR}/site-packages/"
    print_success "Site-packages copied"
}

build_python_deps_image() {
    print_header "Building Python Dependencies Image"

    print_step "Creating Dockerfile for Python dependencies"
    cat > "${BUILD_DIR}/Dockerfile" <<'EOF'
FROM container-registry.oracle.com/os/oraclelinux:9 AS builder

# Install build dependencies
RUN dnf install -y gcc git tar openssl-devel bzip2-devel libffi-devel zlib-devel wget && \
    dnf clean all

# Download and compile Python 3.13.9
RUN wget https://www.python.org/ftp/python/3.13.9/Python-3.13.9.tgz && \
    tar xzf Python-3.13.9.tgz && \
    cd Python-3.13.9 && \
    ./configure --enable-optimizations --prefix=/usr/local && \
    make -j$(nproc) && \
    make install && \
    cd .. && \
    rm -rf Python-3.13.9*

# Final stage
FROM container-registry.oracle.com/os/oraclelinux:9-slim

# Install runtime dependencies and copy Python from builder
COPY --from=builder /usr/local/ /usr/local/

# Set up environment
ENV PATH=/usr/local/bin:$PATH
ENV LD_LIBRARY_PATH=/usr/local/lib
ENV PYTHONPATH=/usr/lib/mysqlsh/python-packages
ENV PYTHONUNBUFFERED=1

# Copy the site-packages directory
COPY site-packages/ /usr/lib/mysqlsh/python-packages/

# Create directory structure
RUN mkdir -p /usr/lib/mysqlsh/python-packages

# Verify the installation
RUN python3 -c "import sys; print(f'Python version: {sys.version}')" && \
    python3 -c "import kubernetes; print(f'Kubernetes: {kubernetes.__version__}')" && \
    python3 -c "import kopf; print(f'Kopf: {kopf.__version__}')" && \
    echo "All dependencies installed successfully!"

# Default command
CMD ["/bin/bash"]
EOF

    print_step "Building ${PYTHON_DEPS_IMAGE}"
    if docker build -t "${PYTHON_DEPS_IMAGE}" "${BUILD_DIR}"; then
        print_success "Python dependencies image built successfully"
    else
        print_error "Failed to build Python dependencies image"
        exit 1
    fi
}

build_operator_image() {
    print_header "Building MySQL Operator Image"

    cd "${CURRENT_DIR}"

    # Check if Dockerfile exists
    if [ ! -f "Dockerfile" ]; then
        print_error "Dockerfile not found in ${CURRENT_DIR}"
        exit 1
    fi

    # Get operator tag
    OPERATOR_TAG=$("${CURRENT_DIR}/tag.sh")
    print_step "Building operator image: ${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64"

    if docker build --build-arg "http_proxy=${http_proxy:-}" \
                    --build-arg "https_proxy=${https_proxy:-}" \
                    --build-arg "no_proxy=${no_proxy:-}" \
                    -f Dockerfile \
                    -t "${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64" .; then
        print_success "Operator image built successfully"
    else
        print_error "Failed to build operator image"
        exit 1
    fi
}

verify_images() {
    print_header "Verifying Built Images"

    # Verify Python deps image
    print_step "Verifying ${PYTHON_DEPS_IMAGE}"
    docker run --rm "${PYTHON_DEPS_IMAGE}" python3 -c "
import sys
import kubernetes
import kopf
print(f'✓ Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')
print(f'✓ Kubernetes: {kubernetes.__version__}')
print(f'✓ Kopf: {kopf.__version__}')
print('✓ urllib3:', end=' ')
import urllib3
print(urllib3.__version__)
"
    print_success "Python dependencies image verified"

    # Verify operator image
    OPERATOR_TAG=$("${CURRENT_DIR}/tag.sh")
    print_step "Verifying ${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64"
    docker run --rm "${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64" sh -c '
echo "✓ Python: $(python3 -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\")")"
echo "✓ MySQL Shell: $(mysqlsh --version | grep -oP \"Ver \K[0-9.]+\")"
echo "✓ Operator code: $(ls /usr/lib/mysqlsh/python-packages/mysqloperator/ | wc -l) files"
'
    print_success "Operator image verified"
}

cleanup() {
    print_header "Cleanup"

    if [ "${CLEANUP_BUILD_DIR:-true}" = "true" ]; then
        print_step "Removing build directory: ${BUILD_DIR}"
        rm -rf "${BUILD_DIR}"
        print_success "Cleanup completed"
    else
        print_step "Keeping build directory: ${BUILD_DIR}"
    fi
}

show_summary() {
    print_header "Build Summary"

    OPERATOR_TAG=$("${CURRENT_DIR}/tag.sh")

    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}  MySQL Operator Build Completed Successfully!${NC}"
    echo -e "${GREEN}=============================================${NC}\n"

    echo "Built Images:"
    echo "  • ${PYTHON_DEPS_IMAGE}"
    echo "  • ${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64"
    echo ""

    echo "Image Sizes:"
    docker images | grep -E "mysql-operator|REPOSITORY" | while read line; do
        echo "  $line"
    done
    echo ""

    echo "Next Steps:"
    echo "  1. Test the operator: docker run --rm ${OPERATOR_IMAGE_TAG}:${OPERATOR_TAG}-amd64 python3 --version"
    echo "  2. Deploy to Kubernetes: kubectl apply -f deploy/deploy-crds.yaml"
    echo "  3. Deploy operator: kubectl apply -f deploy/deploy-operator.yaml"
    echo ""

    echo "To keep build files for debugging, run: CLEANUP_BUILD_DIR=false ./build-all.sh"
    echo ""
}

main() {
    print_header "MySQL Operator One-Click Build"
    echo "This script will build MySQL Operator images with all dependencies."
    echo "Press Ctrl+C to cancel..."
    sleep 2

    check_dependencies
    download_python
    setup_python_environment
    build_python_deps_image
    build_operator_image
    verify_images
    cleanup
    show_summary

    print_success "All done! 🎉"
}

# Parse command line arguments (before main to allow --help to exit quickly)
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cleanup)
            CLEANUP_BUILD_DIR=false
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-cleanup    Keep build directory for debugging"
            echo "  --help          Show this help message"
            echo ""
            echo "This script automates the build process for MySQL Operator images."
            echo "It will download Python, install dependencies, and build both the"
            echo "Python dependencies image and the main MySQL Operator image."
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Run main function
main
