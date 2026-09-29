# Face fixtures

Synthetic portraits for the image leak test. None shows a real person: each is an
AI-generated face (StyleGAN family) that Wikimedia Commons tags `{{PD-algorithm}}`,
meaning it is public domain because no human authored it. Licenses were read from each
file's own Commons page, not from a listing. `{{PD-Robot}}` is a redirect to `{{PD-algorithm}}`.

| File | Source page | License | Why it is not a real person |
|---|---|---|---|
| `boy_1.jpg` | https://commons.wikimedia.org/wiki/File:Boy_1.jpg | Public domain (PD-algorithm) | StyleGAN output; the page says the person does not exist. |
| `man_2.jpg` | https://commons.wikimedia.org/wiki/File:Man_2.jpg | Public domain (PD-algorithm) | StyleGAN output; the page says the person does not exist. |
| `woman_1.jpg` | https://commons.wikimedia.org/wiki/File:Woman_1.jpg | Public domain (PD-algorithm) | StyleGAN output; the page says the person does not exist. |
| `girl_gan.jpg` | https://commons.wikimedia.org/wiki/File:GAN_deepfake_white_girl.jpg | Public domain (PD-algorithm) | GAN-generated; the page describes it as a GAN deepfake, not a photograph. |
| `tpdne_woman.jpg` | https://commons.wikimedia.org/wiki/File:This_Person_Does_Not_Exist_example.jpg | Public domain (PD-Robot, same as PD-algorithm) | StyleGAN2 output from thispersondoesnotexist.com. |
| `stylegan2_person.jpg` | https://commons.wikimedia.org/wiki/File:GAN_Mensch_StyleGAN2.png | Public domain (PD-algorithm) | StyleGAN2 output from thispersondoesnotexist.com. |

Each original is 1024 x 1024. It was resized to 512 x 512, re-encoded as JPEG (quality 85)
and rebuilt from raw pixels, so no EXIF, ICC profile or comment from the original remains.
