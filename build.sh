#!/usr/bin/env zsh

# Make docs
pandoc README.md -o docs/README.rst
# cp assets/* docs/assets/
sphinx-build -b html docs html
rm -r ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client/*
cp html/*.html ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client
cp -r html/_static ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client/_static
cp -r html/_modules ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client/_modules
# cp -r assets/ ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client/assets
touch ~/Developer/jonnybergdahl/jonnybergdahl.github.io/improv-client/.nojekyll