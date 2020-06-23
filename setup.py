# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

from setuptools import setup

setup(
  name="argon1",
  version="0.1",
  packages=['argonone'],

  entry_points={
    "console_scripts": [
      "argonctl = argonone.cmdline:argonctl_main",
      "argononed = argonone.cmdline:argondaemon_main",
    ],
  },

  install_requires=[
    'PyYAML',
    'RPi.GPIO',
    # TODO - python3-smbus deb source is i2c-tools, and package does not show up in pip...
  ],

  # Informational metadata
  author="Spiros Papadimitriou",
  author_email="spapadim@gmail.com",
  description="Alternative implementation of ArgonOne case fan and power control.",
  keywords="argonone raspberrypi",
  url="https://github.com/spapadim/argon1/",   # project home page, if any
  project_urls={
      # "Documentation": "https://docs.example.com/HelloWorld/",
      "Source Code": "https://github.com/spapadim/argon1/",
  },
  classifiers=[
      "Development Status :: 3 - Alpha",
      "Programming Language :: Python :: 3 :: Only",
      "License :: OSI Approved :: MIT License",
      "Topic :: System :: Hardware",
  ]
)
