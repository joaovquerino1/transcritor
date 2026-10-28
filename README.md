# Transcritor

Transcreve vídeos e áudios em qualquer formato, identifica quem está falando e salva o resultado em TXT, Word, SRT, VTT ou JSON. Roda no Windows (x64) e no macOS com Apple Silicon (M1 ou mais novo), **100% local e gratuito**: nenhum arquivo sai do seu computador e não há chave de API.

Otimizado para **português do Brasil** e **inglês** (também tem detecção automática e um modo misto para vídeos que alternam entre os dois).

## Como usar

1. Instale (veja abaixo) e abra o Transcritor.
2. Arraste um ou mais vídeos para a lista (pastas também funcionam).
3. Escolha o idioma e clique em **Transcrever**.
4. Na primeira vez, o programa baixa os modelos de IA (~1,7 GB no modo recomendado). Depois disso funciona offline.
5. Veja o resultado no painel direito. Dá para renomear os falantes ("Falante 1" → "Maria") e clicar em **Salvar arquivos novamente**.

Dicas:
- Se você sabe quantas pessoas falam, informe em **Nº de falantes**: é o que mais melhora a identificação.
- Em modo automático, use **Separação de vozes**: aumente se pessoas diferentes saírem juntas, diminua se a mesma pessoa virar vários falantes.
- Em **Vocabulário**, coloque nomes próprios e siglas que aparecem no vídeo (ex.: `Querino, Itaú, OKR, Kubernetes`).

## Modelos usados (todos gratuitos e abertos)

| Etapa | Modelo | Licença |
|---|---|---|
| Transcrição | OpenAI Whisper large-v3 turbo (via faster-whisper / CTranslate2). Opções: large-v3, medium, small | MIT |
| Detecção de fala | Silero VAD | MIT |
| Segmentação de falantes | pyannote segmentation 3.0 (ONNX, via sherpa-onnx) | MIT |
| Identificação de voz | WeSpeaker ResNet34 (VoxCeleb) | Apache 2.0 |

Os modelos ficam em `~/Library/Application Support/Transcritor/models` (macOS) ou `%LOCALAPPDATA%\Transcritor\models` (Windows).

Desempenho: em CPU, o modo recomendado leva cerca de metade da duração do vídeo num Apple M3 (45 s de vídeo em ~26 s). No Windows, uma GPU NVIDIA com CUDA 12 e cuDNN 9 instalados é usada automaticamente; sem ela, roda na CPU.

## Instalação

- **macOS (Apple Silicon)**: abra `Transcritor-<versão>-macOS-arm64.dmg` e arraste o Transcritor para Aplicativos. Como o app não é assinado com um Developer ID da Apple, na primeira vez clique com o botão direito no app → **Abrir** → **Abrir** (ou em Ajustes do Sistema → Privacidade e Segurança → **Abrir Mesmo Assim**).
- **Windows**: execute `Transcritor-<versão>-Windows-x64-Setup.exe`. O SmartScreen pode avisar que o app é desconhecido: clique em **Mais informações** → **Executar assim mesmo**.

## Gerar os instaladores

Cada instalador precisa ser gerado no próprio sistema. O jeito mais fácil é o GitHub Actions: suba o repositório e crie uma tag (`git tag v1.0.0 && git push --tags`). O workflow [`.github/workflows/build.yml`](.github/workflows/build.yml) gera o `.exe` do Windows e o `.dmg` do macOS para Apple Silicon e publica tudo numa Release.

Manualmente (Python 3.10 a 3.13):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install ".[build]"
pyinstaller packaging/transcritor.spec --noconfirm
```

Depois:
- macOS: `packaging/macos/build_dmg.sh` → `dist/Transcritor-1.0.0-macOS-arm64.dmg`
- Windows: instale o [Inno Setup 6](https://jrsoftware.org/isdl.php) e rode `iscc packaging\windows\installer.iss` → `dist\Transcritor-1.0.0-Windows-x64-Setup.exe`

Para rodar a partir do código: `python run.py`.

## Linha de comando

```bash
Transcritor --cli entrevista.mp4 --idioma pt --falantes 2 --formatos txt,docx,srt
```

No macOS o executável fica em `/Applications/Transcritor.app/Contents/MacOS/Transcritor`. Opções: `--idioma pt|en|auto|mixed`, `--modelo large-v3-turbo|large-v3|medium|small`, `--falantes N`, `--sem-falantes`, `--vocabulario "..."`, `--saida pasta`.

## Estrutura

- `src/transcritor/engine.py`: decodificação de áudio (PyAV/FFmpeg), transcrição, diarização e alinhamento palavra a palavra com os falantes
- `src/transcritor/models.py`: download dos modelos
- `src/transcritor/export.py`: TXT, DOCX, SRT, VTT, JSON
- `src/transcritor/app.py`: interface (PySide6)
- `packaging/`: PyInstaller, DMG, Inno Setup e gerador de ícones
