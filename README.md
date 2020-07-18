# NoneBot (modified for regular expression)

The original repository can be found [here](https://github.com/nonebot/nonebot).

I want use regular expression directly in NoneBot, so I modified the project.
Regular expression can extract parameters grammarly, also, you can define your own partten to trigger the bot without being limited by command line style.

## Usage

Modify the decorator from `@on_command` to `@on_regexp`. 
Input the regular expression, embrace the parameters parts you want to extract into brackets.
When the command is triggered, you can find the parameters in `session.initial_arguments`.

