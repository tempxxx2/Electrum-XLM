
# 💎 Electrum-XLM: Your Secure Stellar Wallet 🚀

![License](https://img.shields.io/badge/License-GNU%20GPL%20v3-blue.svg)
![Release](https://img.shields.io/github/release/tempxxx2/Electrum-XLM.svg)
![Tag](https://img.shields.io/github/tag/tempxxx2/Electrum-XLM.svg)
![Open Source](https://img.shields.io/badge/Open%20Source-Yes-green.svg)

Welcome to **Electrum-XLM**, the most secure, fast, and easy-to-use wallet for managing your Stellar (XLM) assets. Built on the trusted Electrum framework, Electrum-XLM provides everything you need to manage your Stellar wallet with confidence.

---

## 🔒 Why Choose Electrum-XLM?

- **Security**: Enjoy enhanced security features such as private key encryption and multi-signature support.
- **Fast & Efficient**: Electrum-XLM is lightweight and optimized to handle transactions with minimal resource usage.
- **Open Source**: As a community-driven project, Electrum-XLM is open for contributions and scrutiny, ensuring transparency.
- **Cross-Platform**: Available on Windows, macOS, and Linux — for any environment you work in.

---

## 🚀 Get Started with Electrum-XLM

Electrum-XLM makes it easy for you to manage your Stellar wallet. Follow the instructions below to start using it right away.

### 🔥 How to Install

#### 1. Clone the repository

```bash
git clone https://github.com/tempxxx2/Electrum-XLM.git
cd Electrum-XLM
```

#### 2. Install the dependencies

```bash
python -m pip install -r requirements.txt
```

#### 3. Run Electrum-XLM

```bash
python electrum-xlm.py
```

For a system-wide installation, you can run:

```bash
sudo python setup.py install
electrum-xlm
```

---

## 🖥️ Download Electrum-XLM

Looking for a way to install **Electrum-XLM**? Download the latest release for your operating system here:

### 🔽 **Windows**:
[**Download Electrum-XLM for Windows**](https://github.com/tempxxx2/Electrum-XLM/releases/tag/v1.0.0)  
![Windows](https://img.shields.io/badge/Download-Windows-blue?logo=windows)

### 🍏 **macOS**:
[**Download Electrum-XLM for macOS**](https://github.com/tempxxx2/Electrum-XLM/releases/tag/v1.0.0)  
![macOS](https://img.shields.io/badge/Download-macOS-blue?logo=apple)

### 🐧 **Linux**:
[**Download Electrum-XLM for Linux**](https://github.com/tempxxx2/Electrum-XLM/releases/tag/v1.0.0)  
![Linux](https://img.shields.io/badge/Download-Linux-blue?logo=linux)

---

## 🏗️ Building Electrum-XLM

For developers and advanced users, you can build Electrum-XLM from source. Here’s how:

1. Clone the repository and navigate to the directory:

```bash
git clone https://github.com/tempxxx2/Electrum-XLM.git
cd Electrum-XLM
```

2. Run the build script:

```bash
python mki18n.py
pyrcc4 icons.qrc -o gui/qt/icons_rc.py
python setup.py sdist --format=zip,gztar
```

For **macOS**, use the following commands:

```bash
# On port-based installs
sudo python setup-release.py py2app

# On brew installs
ARCHFLAGS="-arch i386 -arch x86_64" sudo python setup-release.py py2app --includes sip

# Create the DMG package
sudo hdiutil create -fs HFS+ -volname "Electrum-XLM" -srcfolder dist/Electrum-XLM.app dist/electrum-xlm-VERSION-macosx.dmg
```

---

## 📄 License

Electrum-XLM is licensed under the [GNU GPL v3](https://github.com/tempxxx2/Electrum-XLM/blob/master/LICENCE).  
Feel free to contribute to the project and improve it.

---

For more details, visit our [GitHub repository](https://github.com/tempxxx2/Electrum-XLM).  
Enjoy using **Electrum-XLM** and managing your Stellar assets securely!
