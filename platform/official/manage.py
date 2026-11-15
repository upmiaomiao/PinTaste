"""Operator-only account setup; passwords are read interactively, never echoed."""
import argparse
import getpass
import sys
import warnings
from pathlib import Path

# Must run as a module (``python -m official.manage``): the sibling modules use
# package-relative imports, so direct script execution cannot resolve them.
from . import store as db
from . import auth


def read_password(minimum_length=8):
    # Refuse fallback echoing if run without an interactive terminal.
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        password=getpass.getpass(f'Password ({minimum_length}-128 characters, hidden): ')
        confirm=getpass.getpass('Confirm password (hidden): ')
    if password!=confirm: raise ValueError('两次输入的密码不一致。')
    return password


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','import-local','create-admin','set-password','set-alias-password'])
    parser.add_argument('--email')
    parser.add_argument('--alias')
    parser.add_argument('--name')
    parser.add_argument('--organization')
    args=parser.parse_args();db.init()
    if args.command=='import-local':
        sys.path.append(str(Path(__file__).resolve().parent.parent/'model-chat'))
        from credentials import api_key,metaso_key
        for name,getter in [('model',lambda:api_key('team')),('search',metaso_key)]:
            if not db.secret(name):
                try:db.save_secret(name,getter());print(name+': imported into encrypted storage')
                except (OSError,ValueError,KeyError):print(name+': not available; configure in administration')
    elif args.command=='set-alias-password':
        alias=args.alias or input('Configured login alias: ').strip()
        auth.set_alias_password(alias,read_password(minimum_length=1))
        print('Alias password updated. Existing sessions were revoked.')
    elif args.command in ('create-admin','set-password'):
        email=args.email or input('Email: ').strip()
        if args.command=='create-admin':
            name=args.name or input('Name: ').strip()
            organization=args.organization or input('Organization: ').strip()
            auth.create_admin(email,read_password(),name,organization)
            print('Administrator created. Sign in with email and password.')
        else:
            auth.set_password(email,read_password())
            print('Password updated. Existing sessions were revoked.')
    else:print('Data directory: '+str(db.DATA))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        from fastapi import HTTPException
        message=exc.detail if isinstance(exc,HTTPException) else str(exc)
        print('Operation failed: '+message,file=sys.stderr)
        sys.exit(1)
