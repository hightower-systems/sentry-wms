"""
CLI utility to generate a bcrypt password hash.

Usage: python scripts/hash_password.py <password>
"""

import sys

import bcrypt


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/hash_password.py <password>")
        sys.exit(1)

    password = sys.argv[1].encode("utf-8")
    hashed = bcrypt.hashpw(password, bcrypt.gensalt())
    print(hashed.decode("utf-8"))


if __name__ == "__main__":
    main()
