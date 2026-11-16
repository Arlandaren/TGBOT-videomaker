# Модели

| файл | что это | лицензия | откуда |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | детектор лица YuNet (кроп, трек лица) | MIT | OpenCV Zoo |
| `modnet_photographic_portrait_matting.onnx` | маска человека MODNet («заголовок за головой», `src/pipeline/matte.py`) | Apache-2.0 | https://huggingface.co/Xenova/modnet (`onnx/model.onnx`), исходник https://github.com/ZHKKKe/MODNet |

Контрольная сумма MODNet закреплена в коде (`matte.MODEL_SHA256`) и проверяется
тестом: файл в репозитории и файл, под который подобраны пороги, — один и тот же.
