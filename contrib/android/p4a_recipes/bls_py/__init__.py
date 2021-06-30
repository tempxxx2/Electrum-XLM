from pythonforandroid.recipe import CythonRecipe
from pythonforandroid.toolchain import shutil
from os.path import join


class BlsPyRecipe(CythonRecipe):

    url = ('https://files.pythonhosted.org/packages/45/b8/'
           '5f98b3c9bc8450ad5b1b10ded618f6a82a9e0288c688d0f4344922bcf423/'
           'python-bls-0.1.9.tar.gz')
    sha256sum = 'f1dddf9f2208591588a167907ef4c13b22f9d8d91d980ca5977eb70961f04154'
    version = '0.1.9'
    depends = ['python3', 'setuptools', 'libgmp']

    def build_arch(self, arch):
        # copy gmp.h from libgmp/dist/include to extmod/bls_py
        self_build_dir = self.get_build_dir(arch.arch)
        libgmp_build_dir = self_build_dir.replace('bls_py', 'libgmp')
        libgmp_build_dir = libgmp_build_dir.replace('-python3', '')
        local_path = join(self_build_dir, 'extmod', 'bls_py', 'gmp.h')
        libgmp_path = join(libgmp_build_dir, 'dist', 'include', 'gmp.h')
        shutil.copyfile(libgmp_path, local_path)
        super(BlsPyRecipe, self).build_arch(arch)


recipe = BlsPyRecipe()
