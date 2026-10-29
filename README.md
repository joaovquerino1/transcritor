# Transcritor

Transcreve vídeos e áudios em qualquer formato, identifica quem está falando e salva o resultado em TXT, Word, SRT, VTT ou JSON. Roda no Windows (x64) e no macOS com Apple Silicon (M1 ou mais novo), **100% local e gratuito**: nenhum arquivo sai do seu computador e não há chave de API.

Também baixa vídeos do **YouTube** e de mais de mil sites, além de **gravações do Microsoft Teams, SharePoint, OneDrive e Stream** (entrando com a sua conta da empresa).

Otimizado para **português do Brasil** e **inglês** (também tem detecção automática e um modo misto para vídeos que alternam entre os dois).

## Como usar

1. Instale (veja abaixo) e abra o Transcritor.
2. Arraste um ou mais vídeos para a janela (pastas também funcionam), ou use **+** / ⌘O.
3. Escolha o idioma e clique em **Transcrever** (⌘↩).
4. Na primeira vez, o programa baixa os modelos de IA (~1,7 GB no modo recomendado). Depois disso funciona offline.
5. Veja o resultado ao lado da lista. Em **Nomes dos falantes** dá para trocar "Falante 1" por "Maria"; depois clique em **Salvar de novo**.

A janela se ajusta a telas pequenas: abaixo de 900 px de largura a barra lateral se recolhe e volta pelo botão no canto superior (⌘⌥S). O tema claro ou escuro acompanha o sistema. As opções menos usadas (número de falantes, separação de vozes, vocabulário, pasta e formatos) ficam em **Mais opções**.

## Baixar da internet

- **YouTube, Vimeo, Instagram, X, Facebook, TikTok e +1000 sites**: cole o link em **Baixar da internet** e clique em **Baixar**. Playlists também funcionam.
- **Gravações do Teams / SharePoint / OneDrive / Stream**: clique em **Entrar no Teams ou SharePoint…** (ou cole o link da gravação). Abre um navegador dentro do app:
  1. Entre com sua conta da empresa (o login fica salvo; há um botão **Sair da conta**).
  2. Abra a gravação e aperte **play**.
  3. Quando aparecer **Vídeo detectado!**, clique em **Baixar vídeo**.

  O app tenta, em ordem: o link do manifesto lido do próprio player do SharePoint, o extrator do SharePoint do yt-dlp com o seu login, e o manifesto capturado na rede (com os cabeçalhos de autorização). A aba **Registro** mostra cada tentativa.

  Se a gravação tiver o botão **Baixar** do próprio SharePoint, ele também funciona nesse navegador.
- Por padrão o app baixa só o áudio (mais rápido e suficiente para transcrever). Marque **Manter o vídeo** para salvar o vídeo em MP4.
- Com **Transcrever ao terminar** marcado, a transcrição começa sozinha depois do download.
- Os arquivos vão para a pasta de **Salvar em** ou, se ela estiver vazia, para `Downloads/Transcritor`.

Respeite as políticas da sua empresa e os direitos dos autores: baixe só o que você tem permissão para usar. Vídeos com DRM não são suportados. Se a sua empresa exigir dispositivo gerenciado no login (acesso condicional), o navegador embutido pode ser bloqueado; nesse caso baixe a gravação pelo navegador normal e arraste o arquivo para o app.

O app traz o Deno embutido: o yt-dlp precisa dele para resolver os desafios de JavaScript do YouTube, e sem ele os downloads do YouTube costumam falhar com HTTP 403.

O componente de download (yt-dlp) se atualiza sozinho uma vez por semana a partir do GitHub oficial do projeto, porque os sites mudam com frequência.

Dicas:
- Se você sabe quantas pessoas falam, informe em **Mais opções › Falantes**: é o que mais melhora a identificação.
- Em modo automático, use **Mais opções › Separação**: aumente se pessoas diferentes saírem juntas, diminua se a mesma pessoa virar vários falantes.
- Em **Vocabulário**, coloque nomes próprios e siglas que aparecem no vídeo (ex.: `Querino, Itaú, OKR, Kubernetes`).

## Gravar reunião

Para transcrever uma chamada (Teams, Zoom, Meet, WhatsApp…) enquanto ela acontece:

1. Em **Gravar reunião**, escolha o que gravar: **Tela**, **Som do computador** (as vozes dos outros participantes) e **Microfone** (a sua voz).
2. Clique em **● Gravar**. Um ícone vermelho aparece na barra de menus (macOS) ou na bandeja (Windows), com a opção **Parar e transcrever**.
3. Ao terminar, clique em **■ Parar e transcrever**. O arquivo `Reunião AAAA-MM-DD HHhMM.mp4` (ou `.m4a`, sem a tela) é salvo e a transcrição começa sozinha com as configurações atuais.

- **macOS**: precisa do macOS 15 ou mais novo. Na primeira vez, autorize em Ajustes do Sistema > Privacidade e Segurança > **Gravação de Tela e Áudio do Sistema** e **Microfone**. Como o app não é assinado pela Apple, pode ser preciso autorizar de novo depois de cada atualização.
- **Windows**: grava o áudio do alto-falante/fone padrão e o microfone padrão. A tela inclui todos os monitores.
- Avise os participantes antes de gravar e siga as regras da sua empresa e a legislação aplicável.

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
Transcritor --cli "https://www.youtube.com/watch?v=..." --idioma auto
Transcritor --cli --gravar 0          # grava a reunião até apertar Enter e transcreve
```

No macOS o executável fica em `/Applications/Transcritor.app/Contents/MacOS/Transcritor`. Opções: `--idioma pt|en|auto|mixed`, `--modelo large-v3-turbo|large-v3|medium|small`, `--falantes N`, `--sem-falantes`, `--vocabulario "..."`, `--saida pasta`, `--gravar SEGUNDOS`, `--sem-tela`, `--sem-microfone`.

## Estrutura

- `src/transcritor/engine.py`: decodificação de áudio (PyAV/FFmpeg), transcrição, diarização e alinhamento palavra a palavra com os falantes
- `src/transcritor/models.py`: download dos modelos
- `src/transcritor/downloader.py`: download de vídeos (yt-dlp + FFmpeg)
- `src/transcritor/login_browser.py`: navegador de login que captura gravações do Teams/SharePoint
- `src/transcritor/export.py`: TXT, DOCX, SRT, VTT, JSON
- `src/transcritor/app.py`: interface (PySide6)
- `src/transcritor/style.py`: cores dos temas claro e escuro, folha de estilo e ícones desenhados
- `src/transcritor/widgets.py`: lista de arquivos, leitura da transcrição, nomes dos falantes e layout que quebra linha
- `packaging/`: PyInstaller, DMG, Inno Setup e gerador de ícones
