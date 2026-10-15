#!/bin/bash
# Create a signed Release archive. This script does not upload or invite testers.
set -euo pipefail

if [[ $# != 1 || ! "$1" =~ ^[1-9][0-9]*$ ]]; then
    echo "Usage: $0 BUILD_NUMBER (a positive integer, higher than the previous upload)" >&2
    exit 2
fi

client_dir="$(cd "$(dirname "$0")/.." && pwd)"
archive_path="$client_dir/.build/testflight/eRecipe-$1.xcarchive"
if [[ -e "$archive_path" ]]; then
    echo "Archive already exists: $archive_path. Choose a new build number or move the existing archive." >&2
    exit 2
fi

python3 "$client_dir/Scripts/generate_project.py"
xcodebuild -project "$client_dir/eRecipe.xcodeproj" -scheme eRecipe \
    -configuration Release -destination 'generic/platform=iOS' \
    -derivedDataPath "$client_dir/.build/testflight/DerivedData" \
    -archivePath "$archive_path" -allowProvisioningUpdates \
    CURRENT_PROJECT_VERSION="$1" archive

echo "Archive ready: $archive_path"
echo "Open it in Xcode Organizer and choose Distribute App > TestFlight Internal Only."
