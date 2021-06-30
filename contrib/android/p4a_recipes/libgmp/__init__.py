from pythonforandroid.recipe import Recipe
from pythonforandroid.toolchain import shprint, current_directory
from multiprocessing import cpu_count
from os.path import join, exists
from os import environ
import sh


class LibGMPRecipe(Recipe):

    url = 'https://gmplib.org/download/gmp/gmp-6.2.1.tar.xz'
    sha256sum = 'fd4829912cddd12f84181c3451cc752be224643e87fac497b69edddadc49b4f2'
    version = '6.2.1'

    def get_recipe_env(self, arch):
        env = environ.copy()
        # BASE_CFLAGS/CFLAGS/LDFLAGS/MPN_PATH from https://github.com/Rupan/gmp
        env['BASE_CFLAGS'] = ('-O2 -fPIC -g -pedantic -fomit-frame-pointer'
                              ' -Wa,--noexecstack -ffunction-sections'
                              ' -funwind-tables -no-canonical-prefixes'
                              ' -fno-strict-aliasing')
        if 'arm64' in arch.arch:
            env['MPN_PATH'] = 'arm64 generic'
            env['LDFLAGS'] = ('-Wl,--no-undefined -Wl,-z,noexecstack'
                              ' -Wl,-z,relro -Wl,-z,now')
            env['CFLAGS'] = (env['BASE_CFLAGS'] +
                             ' -fstack-protector-strong -finline-limit=300'
                             ' -funswitch-loops')
            env['TARGET'] = target = 'aarch64-linux-android'
        else:
            env['MPN_PATH'] = 'arm/v6t2 arm/v6 arm/v5 arm generic'
            env['LDFLAGS'] = ('-Wl,--fix-cortex-a8 -Wl,--no-undefined '
                              '-Wl,-z,noexecstack -Wl,-z,relro -Wl,-z,now')
            env['CFLAGS'] = (env['BASE_CFLAGS'] +
                             ' -fstack-protector -finline-limit=64'
                             ' -march=armv7-a -mfloat-abi=softfp -mfpu=vfp')
            env['TARGET'] = target = 'armv7a-linux-androideabi'
        ndk_api = self.ctx.ndk_api
        ndk_dir = self.ctx.ndk_dir
        clang_path = f'{ndk_dir}/toolchains/llvm/prebuilt/linux-x86_64'
        env['AR'] = f'{clang_path}/bin/llvm-ar'
        env['CC'] = f'{clang_path}/bin/{target}{ndk_api}-clang'
        env['AS'] = env['CC']
        env['CXX'] = f'{clang_path}/bin/{target}{ndk_api}-clang++'
        env['LD'] = f'{clang_path}/bin/ld'
        env['RANLIB'] = f'{clang_path}/bin/llvm-ranlib'
        env['STRIP'] = f'{clang_path}/bin/llvm-strip'
        return env

    def build_arch(self, arch):
        super(LibGMPRecipe, self).build_arch(arch)
        env = self.get_recipe_env(arch)
        with current_directory(self.get_build_dir(arch.arch)):
            dst_dir = join(self.get_build_dir(arch.arch), 'dist')
            shprint(sh.Command('./configure'),
                    '--host={}'.format(env['TARGET']),
                    '--disable-static',
                    '--prefix={}'.format(dst_dir),
                    'MPN_PATH={}'.format(env['MPN_PATH']),
                    _env=env)
            shprint(sh.sed, '-i.bak', '/HAVE_LOCALECONV 1/d',
                    './config.h', _env=env)
            shprint(sh.make, '-j%s' % cpu_count(), _env=env)
            shprint(sh.make, 'install', _env=env)
            libs = ['dist/lib/libgmp.so']
            self.install_libs(arch, *libs)


recipe = LibGMPRecipe()
