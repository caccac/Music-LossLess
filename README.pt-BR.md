# Troca Lossless

[English](README.md)

Ferramenta local para a biblioteca do app Música no Mac.

- **Trocar por Lossless / Atmos**: faixas *Enviadas*, *Correspondidas* ou *Indisponíveis* (que nunca tocam em Lossless/Atmos) são trocadas pela versão do catálogo Apple Music. Álbuns do Apple Music sem Atmos são trocados por outra edição com Dolby Atmos, quando existe.
- **Repetidas**: mesma música, mesmo artista, mesmo álbum, duração até 3 s de diferença. Fica a de melhor qualidade (arquivo sem perda no Mac › Apple Music › Comprado › Correspondido › Enviado; depois maior taxa de bits).

Em toda troca:
1. As antigas que são arquivos seus (enviadas/correspondidas) são **baixadas e copiadas** para `~/Music/Backup versões antigas/Artista/Álbum/`. Se o download falhar, a faixa não é apagada.
2. Reproduções, última reprodução, nota, favorito e "não gostei" passam para a faixa que fica.
3. Nas playlists comuns, a nova entra no lugar da antiga, na mesma posição. Antes disso é salva uma cópia da ordem em `dados/playlists_antes/`.
4. A antiga é apagada da biblioteca (e do iCloud).
5. Tudo fica registrado em `dados/registro.jsonl`.

## Como usar

1. Dê dois cliques em `Abrir.command` ou rode `python3 musica.py`. A página abre em http://localhost:8765.
2. **Chave MusicKit** (uma vez só): em developer.apple.com › Certificates, IDs & Profiles, crie antes um **Media ID** (Identifiers › + › Media IDs, com MusicKit marcado; o identificador começa com `media.`). Depois, em Keys › **+**, marque **Media Services (MusicKit)**, clique em Configure, escolha esse Media ID e baixe o `.p8`. Preencha o Team ID, o Key ID e o caminho do arquivo.
3. **Entrar no Apple Music** e autorizar.
4. **Analisar biblioteca**. Leva alguns minutos na primeira vez; as buscas ficam em cache.
5. Revise a lista. Vêm marcados só os álbuns de confiança **alta**: todas as faixas casaram e o álbum e o artista conferem. Use os filtros e o link "abrir" para checar os outros.
6. **Aplicar**.

Na primeira vez, o macOS pede permissão para o Terminal (ou o app que rodar o script) controlar o app Música.

## Observações

- Faixas sem par no catálogo ficam como estão. A lista aparece no fim da análise.
- Depois de adicionar, o script espera até 10 min pelo iCloud trazer as faixas novas para o Mac. O que não chegar fica "pendente" e pode ser aplicado de novo.
- O token do Apple Music fica em `dados/token_usuario.json` (permissão 600). O `.p8` não é copiado.

## Licença

[MIT](LICENSE)
