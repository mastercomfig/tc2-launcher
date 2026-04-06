#!/usr/bin/env python3
import sys
import re
import os

def get_current_version(pyproject_content):
    match = re.search(r'^version = "(.*?)"$', pyproject_content, flags=re.MULTILINE)
    if not match:
        print("Could not find version in pyproject.toml")
        sys.exit(1)
    return match.group(1)

def bump_version(bump_type_or_version):
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyproject_path = os.path.join(root_dir, "pyproject.toml")
    version_rc_path = os.path.join(root_dir, "version.rc")

    with open(pyproject_path, "r", encoding="utf-8") as f:
        pyproject_content = f.read()

    current_version = get_current_version(pyproject_content)
    print(f"Current version: {current_version}")

    try:
        cur_major, cur_minor, cur_patch = map(int, current_version.split('.'))
    except ValueError:
        print("Current version is not in X.Y.Z format.")
        sys.exit(1)

    if bump_type_or_version == "major":
        new_major, new_minor, new_patch = cur_major + 1, 0, 0
        new_version = f"{new_major}.{new_minor}.{new_patch}"
    elif bump_type_or_version == "minor":
        new_major, new_minor, new_patch = cur_major, cur_minor + 1, 0
        new_version = f"{new_major}.{new_minor}.{new_patch}"
    elif bump_type_or_version == "patch":
        new_major, new_minor, new_patch = cur_major, cur_minor, cur_patch + 1
        new_version = f"{new_major}.{new_minor}.{new_patch}"
    else:
        if not re.match(r"^\d+\.\d+\.\d+$", bump_type_or_version):
            print("Invalid bump argument. Must be 'major', 'minor', 'patch', or a specific version like '1.0.3'.")
            sys.exit(1)
        new_version = bump_type_or_version
        new_major, new_minor, new_patch = map(int, new_version.split('.'))

    print(f"Bumping to version: {new_version}")

    new_version_tuple = f"{new_major}, {new_minor}, {new_patch}, 0"
    new_version_string = f"{new_major}.{new_minor}.{new_patch}.0"

    # Update pyproject.toml
    new_pyproject_content = re.sub(
        r'^version = ".*"$',
        f'version = "{new_version}"',
        pyproject_content,
        flags=re.MULTILINE
    )

    with open(pyproject_path, "w", encoding="utf-8") as f:
        f.write(new_pyproject_content)
    print("Updated pyproject.toml")

    # Update version.rc
    with open(version_rc_path, "r", encoding="utf-8") as f:
        version_rc_content = f.read()

    version_rc_content = re.sub(
        r'filevers=\(\d+,\s*\d+,\s*\d+,\s*\d+\)',
        f'filevers=({new_version_tuple})',
        version_rc_content
    )
    version_rc_content = re.sub(
        r'prodvers=\(\d+,\s*\d+,\s*\d+,\s*\d+\)',
        f'prodvers=({new_version_tuple})',
        version_rc_content
    )
    version_rc_content = re.sub(
        r"StringStruct\(u'FileVersion',\s*u'\d+\.\d+\.\d+\.\d+'\)",
        f"StringStruct(u'FileVersion', u'{new_version_string}')",
        version_rc_content
    )
    version_rc_content = re.sub(
        r"StringStruct\(u'ProductVersion',\s*u'\d+\.\d+\.\d+\.\d+'\)",
        f"StringStruct(u'ProductVersion', u'{new_version_string}')",
        version_rc_content
    )

    with open(version_rc_path, "w", encoding="utf-8") as f:
        f.write(version_rc_content)
    print("Updated version.rc")

    # Update utils.py
    utils_path = os.path.join(root_dir, "tc2_launcher", "utils.py")
    if os.path.exists(utils_path):
        with open(utils_path, "r", encoding="utf-8") as f:
            utils_content = f.read()
        
        utils_content = re.sub(
            r'VERSION\s*=\s*\(\d+,\s*\d+,\s*\d+\)',
            f'VERSION = ({new_major}, {new_minor}, {new_patch})',
            utils_content
        )
        
        with open(utils_path, "w", encoding="utf-8") as f:
            f.write(utils_content)
        print("Updated utils.py")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python bump_version.py <major|minor|patch|X.Y.Z>")
        print("Example: python bump_version.py patch")
        print("Example: python bump_version.py 1.0.3")
        sys.exit(1)
    bump_version(sys.argv[1])
