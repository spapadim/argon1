#!/bin/bash
# Utility script to generate debian changelog from git log

# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.


# Auxiliary functions

msg() {
  echo "$@" 1>&2
}

die() {
  msg "$@"
  exit 1
}

usage() {
  echo <<EOF 1>&2
Usage: $0 [-l | -nNUM ] [-r] [-a] [HEADVERSION]
  -nNUM keeps only NUM latest versions
  -l    shorthand for -n1
  -r    replace current debian/changelog contents; if missing, prepends
  -a    also include commits with "#nodch" in subject
  -h    this help message
EOF
}

check_version_tag() {
  [[ $1 =~ ^v[[:digit:]]+\.[[:digit:]]+(\.[[:digit:]]+)?$ ]]
}


# Parse arguments and validate inputs

[ -d debian ] || die "ERROR: debian folder does not exist; is pwd wrong?"

[ -f debian/control ] || die "ERROR: debian/control does not exist"
pkgname=`cat debian/control | grep '^Package: ' | sed 's/^Package: //'`

replace_changelog=0
keep_last=""
gitlog_dch_filter_args="--grep=#nodch\b --invert-grep"
while getopts ":hln:ra" opt; do
  case $opt in
    a)
      gitlog_dch_filter_args=""
      ;;
    n)
      [ -z "$keep_last" ] || die "ERROR: Cannot specify -n/-l multiple times"
      keep_last=$OPTARG
      ;;
    l)
      [ -z "$keep_last" ] || die "ERROR: Cannot specify -n/-l multiple times"
      keep_last=1
      ;;
    r)
      replace_changelog=1
      ;;
    h)
      usage
      exit 0
      ;;
    \?)
      die "ERROR: Invalid option -$OPTARG"
      ;;
    :)
      die "ERROR: Option -$OPTARG requires an argument"
      ;;
  esac
done
shift $((OPTIND-1))

head_versiontag="$1"

if [ -n "$keep_last" ]; then  # XXX - no short-circuit eval by test?
  if [ "$keep_last" -eq 1 -a -z "$head_versiontag" ]; then
    msg "WARNING: Last version only with no future tag for HEAD; are you sure?"
  fi
fi

# TODO - should we make -r and -n/-l mutually exclusive ?


# Scan git version tags

tags=("")  # ignored; added just so indices of parallel arrays line up
revs=(`git rev-list --max-parents=0 HEAD`)  # initial revision
# Find previous versions
for version_tag in `git tag -l v* | sort -V`; do
  check_version_tag "$version_tag" || die "ERROR: Invalid git version tag $version_tag"
  tags+=("$version_tag")
  revs+=(`git rev-parse "$version_tag"`)
done
# Add entry for next (planned) version, if specified
if [ -n "$head_versiontag" ]; then
  check_version_tag "$head_versiontag" || die "ERROR: Invalid planned version tag format $head_versiontag"
  tags+=("$head_versiontag")
  revs+=(`git rev-parse HEAD`)
else
  msg "WARNING: Will not add version info for HEAD"
fi

# length of revs; always one more than number of versions in changelog
num_revs=${#revs[@]}

if [ "$num_revs" -lt 2 ]; then
  die "ERROR: No prior versions and no planned tag for HEAD; nothing to output!"
fi

if [ -n "$keep_last" ]; then   # XXX - test doesn't do short-circuit ?
  if [ "$num_revs" -le "$keep_last" ]; then
    die "ERROR: Fewer actual versions than the number you asked to keep"
  fi
fi

# Construct output

if [ -z "$keep_last" ]; then
  keep_last=$((num_revs-1))  # keep all
fi

for (( i=$((num_revs-keep_last)); i < $num_revs; i++ )); do
  version="${tags[$i]#v}"
  full_rev_range="${revs[$i-1]}..${revs[$i]}"
  last_rev_range="${revs[$i]}^..${revs[$i]}"  # Just last revision
  echo -e "$pkgname ($version) unstable; urgency=low\n"
  git log --pretty='format:  * %s' $gitlog_dch_filter_args "$full_rev_range"
  # We semi-arbitrarily pick commiter of version tag revision as maintainer
  git log --pretty='format:%n -- %aN <%aE>  %aD%n%n' "$last_rev_range"
done

# Append current debian/changelog, if present and not in overwrite mode
if [ -f debian/changelog -a "$replace_changelog" -ne 1 ]; then 
  cat debian/changelog
fi

