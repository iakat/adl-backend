# adsb.lol Aircraft Data Link Collector

## 🏗️ Architecture

```
Aircraft Decoders → TCP Ports → File Rotation → Compression → GitHub Release Upload
                    5550/5551/5552     ↓           ↓              ↓
                                   1hr/512MB → zstd → Release Asset
```
