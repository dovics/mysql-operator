#!/bin/bash
# Copyright (c) 2021, 2025 Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#

COMMIT_ID=$(git rev-parse --short HEAD 2>/dev/null || echo "")
SUFFIX=''; [ -n "$1" ] && SUFFIX=${1}

if [ -n "$COMMIT_ID" ]; then
    echo "9.7.0-2.2.7-${COMMIT_ID}${SUFFIX}"
else
    echo "9.7.0-2.2.7${SUFFIX}"
fi
