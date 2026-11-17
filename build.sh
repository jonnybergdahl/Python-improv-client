#!/usr/bin/env zsh
setopt rm_star_silent

SITE=~/Developer/jonnybergdahl/jonnybergdahl.github.io
DOCS=$SITE/improv-client

# Run tests (the venv has the serial and BLE extras installed)
venv/bin/python -m pytest -q || exit

# Make docs
pandoc README.md -o docs/README.rst
rm -rf html
sphinx-build -b html docs html || exit
mkdir -p $DOCS
rm -rf $DOCS/*
cp -R html/ $DOCS
touch $DOCS/.nojekyll

# Commit and push docs
pushd $SITE || exit
git checkout main
git pull
git add improv-client
if [ -n "$(git status --porcelain)" ]; then
  git commit -m "Update improv-client docs"
  git push
else
   echo "Branch main is up to date,nothing to do."
fi
popd || exit

# Make new version
rm -rf build dist
venv/bin/python -m build
venv/bin/python -m twine check dist/* || exit
venv/bin/python -m twine upload dist/*
